"""
Rebuild executor — updates a running doodba instance via ``copier update``.

Flow:
  1. Upload the same copier answer files as a fresh deploy (answers.yml,
     repos.yaml, addons.yaml, backup.env, odoo.conf, pip/apt, ic_connect).
  2. Commit any dirty files in the instance git repo so copier finds a clean
     working tree (required by copier update).
  3. Run ``copier update`` — regenerates every config file from the current
     template version and the new answers.  This is the single source of
     truth for all configuration: SMTP, domain, backup vars, etc.
  4. Post-copier fixups identical to the deploy executor:
     - Fix docker-compose.yml symlink
     - Overwrite backup.env (adds AWS_ENDPOINT_URL which copier doesn't know)
     - Overwrite repos.yaml / addons.yaml / odoo.conf / pip / apt
     - Copy incubacloud_connect
  5. Rebuild the Odoo Docker image and restart.
"""
from datetime import datetime, timedelta, timezone

from odoo import fields

from odoo.addons.queue_job.exception import RetryableJobError
from .host_build_lock import HostBuildLockMixin

from ._odoo_versions import headless_flag
from .abstract_executor import CANCELLED_WHILE_RUNNING, sql_escape_literal
from .deploy_instance_executor import DeployInstanceExecutor
from .docker_prune_executor import build_cache_prune_if_low

# ``cron_guard.sh`` exits with this when the instance still had crons
# executing after its budget: nothing was updated, so the job is worth
# retrying rather than failing.
_EXIT_CRONS_BUSY = 75
#: How long a rebuild keeps coming back while the instance's crons run,
#: counted from its first attempt. Past it the job fails for good.
_CRONS_BUSY_PATIENCE = timedelta(minutes=30)
#: Seconds between two attempts of a rebuild the crons held back. Each
#: attempt starts over — the copier, an image build without cache, the
#: boot test — so they are five minutes apart: within the half hour of
#: patience that is six of them, not thirty.
_CRONS_BUSY_RETRY_S = 300
#: What a failed image build waits before its one repetition, in seconds.
_BUILD_RETRY_WAIT_S = 60
#: The step that rewrites the stack's files on the host, and the one that
#: puts what they describe in service (``on_cancelled_running``).
_LABEL_COPIER = "Update with copier"
_LABEL_BUILD = "Rebuild Odoo image"
#: The step that updates the live database to the new image's modules:
#: Odoo commits module by module, so it is the rebuild's point of no
#: return (``_no_return_from``).
_LABEL_UPDATE = "Update changed modules"
_LABEL_RESTART = "Restart instance"


def _build_retried_once(build):
    """Return a shell command that repeats a failed image build once.

    The build clones every repository of the image, so a passing failure
    at GitHub would otherwise fail the whole rebuild (QA, 31-oct-2026). The
    repetition also runs for a build that fails for good: it only costs that
    build's time, and the job then fails with the same error.

    :param str build: the ``docker compose build`` command to run.
    :return: the command, exiting with the status of its last attempt.
    :rtype: str
    """
    return (
        f"{{ {build} || {{"
        f" echo 'The image build failed: trying once more"
        f" in {_BUILD_RETRY_WAIT_S} s.';"
        f" sleep {_BUILD_RETRY_WAIT_S}; {build}; }}; }}"
    )


class RebuildInstanceExecutor(HostBuildLockMixin, DeployInstanceExecutor):
    """Rebuild a running doodba instance using copier update.

    ``HostBuildLockMixin`` is first in the bases so its
    ``pre_run_checks`` runs and chains: this is the only class in the
    codebase that issues ``docker compose build``, and every rebuild
    variant inherits from it, so putting the per-host build lock here
    covers manual, tenant and warm rebuilds at once.
    """

    _job_type = "rebuild_instance"
    # Stopped halfway, the update leaves the database half updated under
    # the previous image, and there is no copy to go back to: from it on
    # a stop is refused. A deploy has none: a new instance stopped
    # halfway is removed.
    _no_return_from = _LABEL_UPDATE

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("rebuild_instance job has no instance_id")
        inst._check_template_ref(self._copier_template()[1])
        self._preflight_tls_check()
        self._sys(f"Preparing rebuild for '{inst.name}'...")
        await self._upload_copier_files(transport)
        await self._ship_secret_key(transport)
        await self._stage_extra_env_files(transport)

    def get_commands(self):
        inst = self._inst()
        name = inst.doodba_project_name
        d = self._inst_dir(inst)
        src = f"{d}/odoo/custom/src"
        confd = f"{d}/odoo/custom/conf.d"

        tmp_answers = self._tmp("answers.yml")
        tmp_addons = self._tmp("addons.yaml")
        tmp_repos = self._tmp("repos.yaml")
        tmp_conf = self._tmp("odoo.conf")
        tmp_pip = self._tmp("pip.txt")
        tmp_apt = self._tmp("apt.txt")

        compose_target = (
            "prod.yaml" if inst.environment == "production" else "test.yaml"
        )
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        has_smtp = bool(inst.smtp_relay_host)
        needs_full_rebuild = (
            inst.rebuild_fingerprint != inst.last_rebuild_fingerprint
        )
        if needs_full_rebuild:
            self._sys(
                "Full rebuild: configuration changed since last deploy."
            )
        else:
            self._sys(
                "Fast rebuild: only code changes detected, using Docker cache."
            )

        cmds = [
            # 1. Commit any dirty files so copier update finds a clean repo.
            (
                "Commit dirty files",
                self.run_script("rebuild.sh", ["commit-dirty", d, ts]),
            ),
            # 2. Run copier update — regenerates all config files.
            (
                _LABEL_COPIER,
                self.run_script(
                    "rebuild.sh",
                    ["copier-update", d, tmp_answers,
                     self._copier_template()[1]],
                    env=self._copier_settings_env(),
                ),
            ),
            # 2b. Resolve copier merge conflicts: keep new version (after =======)
            (
                "Resolve merge conflicts",
                self.run_script("rebuild.sh", ["resolve-conflicts", d]),
            ),
            # 3. Fix docker-compose.yml symlink (copier points to devel.yaml).
            #    Left inline: a trivial rm + ln, with the shell expanding ~.
            (
                "Fix docker-compose symlink",
                f"rm -f {d}/docker-compose.yml"
                f" && ln -s {compose_target} {d}/docker-compose.yml",
            ),
        ]

        # 3b. Strip the smtp service from prod.yaml when no SMTP relay
        #     is configured (shared with deploy via the same script).
        if not has_smtp and inst.environment == 'production':
            cmds.append((
                "Strip smtp service (not configured)",
                self.run_script(
                    "strip_compose_service.sh", [d, "smtp", "prod.yaml"],
                ),
            ))

        if not self._backup_enabled() and inst.environment == 'production':
            cmds.append((
                "Strip backup service (not configured)",
                self.run_script(
                    "strip_compose_service.sh",
                    [d, "backup", "prod.yaml", "common.yaml"],
                ),
            ))

        # 3c. Cap every container hostname at 64 bytes (see the deploy
        #     executor for the full rationale). ``copier update`` regenerates
        #     the compose files, so this runs on every rebuild too.
        cmds.append((
            "Cap container hostnames",
            self.run_script("deploy.sh", ["cap-hostnames", d]),
        ))

        cmds += [
            # 4. Overwrite backup.env with ours (adds AWS_ENDPOINT_URL).
            (
                "Write backup.env",
                f"f={self._tmp('backup.env')};"
                f" [ -f \"$f\" ] && mv \"$f\" {d}/.docker/backup.env || true",
            ),
            *self._write_extra_env_step(d),
            # 4b. Write the panel's copy of the encryption key (merged
            #     with the host's in ``before_execute``), then make sure
            #     the file exists.
            self._write_secret_key_step(d),
            (
                "Ensure incubacloud.env",
                self.run_script("deploy.sh", ["ensure-secret-key", d]),
            ),
            # 4c. Re-inject incubacloud.env in prod.yaml and test.yaml —
            #     copier update regenerates them, stripping our addition.
            (
                "Inject incubacloud.env in prod.yaml and test.yaml",
                self.run_script("deploy.sh", ["inject-secret-env", d]),
            ),
            # 4d. Write docker-compose.override.yml: resource limits,
            #     protect label and container log rotation.
            (
                "Write compose override",
                f"f={self._tmp('override.yml')};"
                f" [ -f \"$f\" ] &&"
                f" mv \"$f\" {d}/docker-compose.override.yml || true",
            ),
            # 5. Overwrite repos.yaml and addons.yaml.
            ("Write addons.yaml", f"mv {tmp_addons} {src}/addons.yaml"),
            ("Write repos.yaml", f"mv {tmp_repos} {src}/repos.yaml"),
            # 6. Copy incubacloud_connect, over no copy of an older one:
            #    ``cp -r`` would leave behind what this one no longer
            #    ships, like the tests/ an instance got before 7-nov-2026.
            (
                "Install incubacloud_connect",
                f"mkdir -p {src}/private"
                f" && rm -rf {src}/private/incubacloud_connect"
                f" && cp -r {self._modules_stage_dir()}/incubacloud_connect"
                f" {src}/private/"
                f" && rm -rf {self._modules_stage_dir()}",
            ),
            *self._debian_archive_steps(d),
            # 7. Write Odoo conf, pip.txt, apt.txt.
            (
                "Write Odoo conf",
                f"mkdir -p {confd} && mv {tmp_conf} {confd}/{name}.conf",
            ),
            (
                "Write pip.txt",
                f"mkdir -p {d}/odoo/custom/dependencies"
                f" && mv {tmp_pip} {d}/odoo/custom/dependencies/pip.txt",
            ),
            (
                "Write apt.txt",
                f"mv {tmp_apt} {d}/odoo/custom/dependencies/apt.txt",
            ),
            # 8. Remove the temp answers and copier settings files.
            (
                "Remove temp files",
                f"rm -f {tmp_answers} {self._tmp(self._COPIER_SETTINGS)}",
            ),
            # 9. Capture services list for on_success (parsed from stdout).
            ("List services", f"cd {d} && docker compose config --services"),
            # 9b. Empty Docker's build cache first when the host's disk
            #     is low, as a deploy does. Never fails the rebuild.
            (
                "Free the build cache if the disk is low",
                build_cache_prune_if_low(),
            ),
            # 10. Rebuild Odoo image.
            #     Smart rebuild: full (--pull --no-cache) when config that
            #     affects the image has changed; cached otherwise. A cached
            #     one still clones a branch that moved: the heads file
            #     written first is in the build context (5-nov-2026). One
            #     step with the build, so a layer that drops the build
            #     drops the lookup with it.
            (
                _LABEL_BUILD,
                f"{self._record_heads_command(d)} && cd {d} && "
                + _build_retried_once(
                    "docker compose build --pull --no-cache odoo"
                    if needs_full_rebuild else "docker compose build odoo"
                ),
                {"stop_on_failure": True},
            ),
        ]
        # 11 + 12: safe boot test and actual module update both rely
        # on ``click-odoo-update``. When ``auto_update`` is disabled
        # the operator manages module state manually, so skipping both
        # avoids surprising DB migrations — at the cost of losing the
        # boot validation. ``inst.auto_update`` defaults to True for
        # all pre-existing instances, preserving current behavior.
        if inst.auto_update:
            cmds += [
            # 11. Safe boot test: clone the production DB, boot Odoo
            #     with the new image against the clone, and clean up.
            #     If the boot fails, stop_on_failure prevents up -d
            #     so the instance keeps running with the old image.
            (
                "Test new image (safe boot check)",
                self.run_script(
                    "rebuild.sh",
                    [
                        "boot-test", d, inst.id, name,
                        inst.postgres_username or "odoo",
                        inst.postgres_version or "17",
                        inst.postgres_dbname or "prod",
                    ],
                ),
                {"stop_on_failure": True},
            ),
            # 12. Update changed modules in the real DB.
            #     click-odoo-update compares checksums stored in the DB
            #     against the code in the new image and updates only
            #     modules that actually changed. Wrapped by
            #     ``cron_guard.sh``: Odoo refuses to modify an ir_cron
            #     row whose job is running, so an update that ships a
            #     cron record used to die on a ParseError whenever a
            #     tick was in flight. The guard pauses the crons, waits
            #     for the in-flight ones, and always restores them.
            (
                _LABEL_UPDATE,
                self.run_script(
                    "cron_guard.sh",
                    [
                        "update", d,
                        inst.postgres_dbname or "prod",
                        inst.postgres_username or "odoo",
                    ],
                ),
                {"stop_on_failure": True},
            ),
            ]
        cmds += [
            # 12b. Ensure incubacloud_connect is installed (restored DBs
            #      may not have it; click-odoo-update only updates, not
            #      installs). The script asks the database first, and
            #      boots no Odoo for a module already there.
            (
                "Ensure incubacloud_connect",
                self.run_script(
                    "restore.sh",
                    [
                        "ensure-connect", d,
                        inst.postgres_username or "odoo",
                        inst.postgres_dbname or "prod",
                        headless_flag(inst.odoo_version),
                    ],
                ),
            ),
            # 12b. Ready ``logs/`` (and its logrotate config) before the
            #      stack comes back up: Docker creates a missing
            #      bind-mount source as root, and Odoo could not write
            #      to it.
            self._prepare_logs_step(d),
            # 13. Restart all services with the new image.
            #     --remove-orphans drops containers no longer in the compose
            #     file (e.g. backup or smtp when the operator removes the
            #     corresponding service from the instance config) so they
            #     don't linger.
            (
                _LABEL_RESTART,
                f"cd {d} && docker compose up -d --remove-orphans",
            ),
            # 13. Set web.base.url and report.url in ir.config_parameter
            #     (shared with deploy via the same script; base_url arrives
            #     already sql-escaped).
            (
                "Set system parameters",
                self.run_script(
                    "deploy.sh",
                    [
                        "set-system-params", d,
                        inst.postgres_username or "odoo",
                        inst.postgres_dbname or "prod",
                        sql_escape_literal(self._base_url()),
                        "http://localhost:8069",
                    ],
                ),
            ),
        ]
        return cmds

    def parse_results(self, results):
        # A rebuild blocked by a running cron is a transient collision,
        # not a broken instance: nothing was updated and the live stack
        # was never touched. Ask queue_job to come back later instead of
        # failing the job — a failure here would raise an alert (and
        # notify the customer) for something that fixes itself.
        #
        # ``ignore_retry``: every rebuild is queued with ``max_retries=1``,
        # so a plain ``RetryableJobError`` was queue_job's last attempt
        # and ended the job failed. The patience below is the cap instead;
        # meanwhile the job stays pending at the head of its queue.
        update = results.get("Update changed modules", {})
        if update.get("exit_status") == _EXIT_CRONS_BUSY:
            waited = fields.Datetime.now() - self._first_attempt_started_at()
            if waited >= _CRONS_BUSY_PATIENCE:
                return [
                    "The instance's crons kept running for 30 minutes;"
                    " nothing was updated."
                ]
            raise RetryableJobError(
                "Instance crons were still running; rescheduling the "
                "rebuild.",
                seconds=_CRONS_BUSY_RETRY_S,
                ignore_retry=True,
            )
        # "List services" exit_status != 0 only means compose isn't configured;
        # don't treat it as a fatal error — the rebuild itself may have worked.
        ignored = {"List services"}
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get("exit_status", 1) != 0 and label not in ignored
        ]

    def _first_attempt_started_at(self):
        """Return when this job's first attempt began.

        Neither record keeps it: queue_job blanks ``date_started`` each
        time it postpones the job, and ``cloud.job`` has no start of its
        own. What outlives the rollback of a retried attempt is its log,
        which every attempt writes on a durable cursor from its first
        line. The earliest chunk written since the job's ``queue.job``
        was created is the first attempt's: notes logged before, while
        the job waited for its turn or for an alert, are left out.

        The first attempt does not see its own chunks (its snapshot was
        taken before them), so it counts from now.

        :return: naive UTC datetime
        """
        domain = [("job_id", "=", self.job.id)]
        created = self.job.sudo().queue_job_id.date_created
        if created:
            domain.append(("create_date", ">=", created))
        first = self.env["cloud.job.log.chunk"].sudo().search(
            domain, order="id", limit=1,
        )
        return first.create_date or fields.Datetime.now()

    async def on_success(self, results):
        self._cancel_stale_waits()
        inst = self._inst()
        # The template this rebuild rendered with: from now on the stack's.
        write_vals = {
            "status": "ok",
            "stack_template_ref": self._copier_template()[1] or False,
        }
        services_out = (
            results.get("List services", {}).get("stdout") or ""
        )
        services = [s.strip() for s in services_out.splitlines() if s.strip()]
        if services:
            write_vals["compose_services"] = ",".join(services)
        # Save rebuild fingerprint for smart rebuild decision next time
        write_vals['last_rebuild_fingerprint'] = (
            inst.rebuild_fingerprint
        )
        # Config-drift anchor: this rebuild just shipped exactly the
        # current snapshot, so the saved config is applied. Without this
        # only full deploys re-anchored, and a drift-curing rebuild left
        # the config_dirty pill lit forever.
        write_vals.update(inst._applied_config_vals())
        # Its restart step brought the stack up (``docker compose up -d``):
        # it says so, as a deploy does, and the metrics cron gives the
        # recreated odoo's series time to arrive. Not for a stack whose
        # stop is expected: a layer above that sleeps idle instances
        # hands the rebuilt stack back stopped.
        if not inst._stop_is_expected():
            write_vals.update(inst._running_confirmed_vals())
        inst.write(write_vals)
        self._sys(
            f"✓ '{inst.name}' rebuilt and restarted"
            f" with updated configuration."
        )
        self._call_addon_hook(inst, "_after_stack_rendered")

    #: The payload keys that describe one GitHub push.
    _PUSH_KEYS = (
        'push_repo', 'push_branch', 'push_sha', 'push_message', 'push_by',
    )

    @classmethod
    def _merge_queued_payload(cls, waiting_job, payload):
        """A push merges into the rebuild of a push already waiting.

        A rebuild asked for by a GitHub push (``trigger: webhook``) while
        one of the same origin waits on the instance does not queue a
        second one: the waiting job's list of pushes grows with the new
        one, under ``coalesced_pushes``, and its head — the push_* fields
        a job list shows — stays the first push. Pushes to different
        repositories of the instance merge too, on purpose: a rebuild
        pulls every repository's branch head when it runs, so one build
        covers them all and every push stays visible in the job. A
        rebuild someone asked for by hand, or one already dispatched, is
        not merged into.

        :param waiting_job: a ``queued`` rebuild of the instance
        :param dict payload: the new request's payload
        :return: whether the push was absorbed
        :rtype: bool
        """
        if payload.get('trigger') != 'webhook':
            return False
        waiting = dict(waiting_job.payload or {})
        if waiting.get('trigger') not in ('webhook', 'coalesced'):
            return False
        push = {key: payload.get(key, '') for key in cls._PUSH_KEYS}
        pushes = list(waiting.get('coalesced_pushes') or [])
        if not pushes:
            pushes.append({key: waiting.get(key, '') for key in cls._PUSH_KEYS})
        pushes.append(push)
        waiting.update(trigger='coalesced', coalesced_pushes=pushes)
        # Under the queue's lock, but on this transaction's snapshot: if a
        # dispatcher started the job after the snapshot was taken,
        # PostgreSQL refuses this update (a serialization failure), the
        # transaction rolls back and is retried (an HTTP request by Odoo,
        # a queue job by queue_job), finding the job no longer waiting.
        waiting_job.write({'payload': waiting})
        return True

    @classmethod
    def _pushed_shas(cls, payload):
        """Return the commits a push's rebuild was asked for, as pushed.

        The head push and the ones merged into it
        (``coalesced_pushes``), by the short sha the webhook stores.
        Nothing for a rebuild no push asked for.

        :param dict payload: a rebuild's payload
        :rtype: set
        """
        payload = payload or {}
        if payload.get('trigger') not in ('webhook', 'coalesced'):
            return set()
        pushes = [payload, *(payload.get('coalesced_pushes') or [])]
        return {
            push.get('push_sha') for push in pushes
            if isinstance(push, dict) and push.get('push_sha')
        }

    @classmethod
    def _supersedes_failure(cls, waiting_job, failed_jobs):
        """A new push replaces the failed rebuilds holding the queue.

        Someone whose push broke the build pushes the fix: its rebuild
        is a new attempt, not something that would only fail behind the
        failure, and it starts without anybody dismissing the alert
        first. Only when every failure holding the queue is a rebuild of
        the same type (asked for by a push or by hand) and the waiting
        rebuild carries a commit none of them was asked for: the same
        push delivered again replaces nothing. A rebuild asked for by
        hand, and a retry, replace nothing either: the retry passes as
        one (``cloud.job._passes_hold``), and a person who wants the
        branch rebuilt as it is retries the failure. Its success
        resolves the alerts (``cloud.job._dismiss_job_failed_alerts``);
        its failure refreshes the same ``job_failed``.

        :param waiting_job: a ``queued`` rebuild of the instance
        :param failed_jobs: the failed jobs whose alerts hold its queue
        :rtype: bool
        """
        if waiting_job.retry_of_id:
            return False
        pushed = cls._pushed_shas(waiting_job.payload)
        if not pushed or not failed_jobs:
            return False
        if any(job.job_type_id != waiting_job.job_type_id for job in failed_jobs):
            return False
        tried = set().union(*(cls._pushed_shas(job.payload) for job in failed_jobs))
        return bool(pushed - tried)

    async def on_abort(self, exc):
        """A rebuild that ended without an outcome leaves the record alone.

        Most often nothing ran (a refused check, a host that did not
        answer) and the instance still runs its last build: the deploy's
        ``on_abort`` would mark it in error. The job log says what
        happened, and what an attempt that got further left on the host
        (``_warn_unapplied``) — a run whose worker died, closed by the
        reconciliation; the next probe says how the instance is.
        """
        self._sys(f"✗ {type(exc).__name__}: {exc}")
        self._warn_unapplied(None)

    async def on_cancelled_running(self, results, exc):
        """A rebuild stopped by hand leaves the instance's status alone.

        Until its restart step the instance runs its last build: the image
        build and the boot test touch no live container, so the failure's
        ``status: error`` said something untrue of a working instance
        (6-nov-2026). What was left on the host is written to the log
        instead (``_warn_unapplied``), and the instance's state is read
        again now (``_refresh_state_now``): ``cloud.job`` asks for no
        probe after a cancellation, and one that came after the restart
        started has changed it.

        :param results: what the steps answered, or None before them
        :param JobCancelled exc: the stop
        """
        ran = list(results or ())
        self._sys(
            f"✗ {CANCELLED_WHILE_RUNNING} Steps run: {', '.join(ran)}."
            if ran else f"✗ {CANCELLED_WHILE_RUNNING}"
        )
        if not self._warn_unapplied(results):
            self._sys(
                "The instance's status is unchanged; its state is read"
                " again now."
            )
        inst = self._inst()
        if inst:
            inst._refresh_state_now()

    def _warn_unapplied(self, results):
        """Say in the log what a rebuild that ended unapplied left on the host.

        ``copier update`` rewrites the stack's files in place — the
        compose files and their link, the Odoo conf, ``repos.yaml``,
        ``addons.yaml``, ``pip.txt``, the env files, the override — and
        «Rebuild Odoo image» retags the image, which neither the boot test
        nor «Update changed modules» has passed yet. Ended between the
        first and «Restart instance», the instance runs its previous
        build, but whatever recreates its containers (``docker compose up``:
        a Start, a restore, a move) would start them with those files and
        that image. Only a warning, decided on 6-nov-2026: a rebuild that
        fails at the same point (the build, the boot test) leaves the
        same, and a block that covers both belongs on the host, where
        ``compose_op.sh up`` would honour it whatever closed the job.
        Ended once «Update changed modules» had started (a worker that
        died there, a stop that came without the job's lock), the database
        may be ahead of the image that runs: Odoo stores each module it
        updates as it goes.

        :param results: what the steps answered, or None
        :return: whether there was something to say
        :rtype: bool
        """
        left = self._left_unapplied(results)
        if left == _LABEL_UPDATE:
            self._sys(
                "⚠ This rebuild updated the modules of the instance's"
                " database, or began to (Odoo stores them one by one), to"
                " the image it built, and ended before restarting the"
                " instance: its running containers still have the previous"
                " image over that database. Rebuild it to put the new image"
                " in service."
            )
        elif left == _LABEL_BUILD:
            self._sys(
                "⚠ This rebuild rewrote the instance's files on its server"
                " (copier) and may have rebuilt its image, which was neither"
                " boot-tested nor applied to its database's modules; it"
                " ended before restarting the instance, which runs its"
                " previous build. Starting or restoring the instance would"
                " recreate its containers with them: rebuild it first."
            )
        elif left == _LABEL_COPIER:
            self._sys(
                "⚠ This rebuild rewrote the instance's files on its server"
                " (copier) and ended before restarting it: the instance runs"
                " its previous build. Starting or restoring it would"
                " recreate its containers with the rewritten files: rebuild"
                " it first."
            )
        return bool(left)

    def _left_unapplied(self, results):
        """Return how far a rebuild got past copier without a restart after.

        The last of «Update with copier», «Rebuild Odoo image», «Update
        changed modules» and «Restart instance» that any attempt of the
        job started, read from
        the job log's "Running:" lines (each attempt's, a step cut off
        included), unless *results* holds the restart. Whether the
        database moved on is the job's mark (``_database_left_ahead``),
        not that last line: an attempt that began the update and died,
        then one cut short in copier, leave the database ahead all the
        same.

        :param results: what the steps answered, or None
        :return: ``_LABEL_UPDATE``, ``_LABEL_BUILD`` or ``_LABEL_COPIER``,
            or None when nothing is left unapplied
        """
        if results and _LABEL_RESTART in results:
            return None
        lines = {f"Running: {label}": label for label in (
            _LABEL_COPIER, _LABEL_BUILD, _LABEL_UPDATE, _LABEL_RESTART,
        )}
        last = self.env["cloud.job.log.chunk"].sudo().search(
            [("job_id", "=", self.job.id), ("content", "in", list(lines))],
            order="id desc", limit=1,
        )
        label = lines.get(last.content)
        if label == _LABEL_RESTART:
            return None
        if label != _LABEL_UPDATE and self._database_left_ahead():
            return _LABEL_UPDATE
        if label is None and results and _LABEL_COPIER in results:
            # The log was capped before the line.
            for later in (_LABEL_UPDATE, _LABEL_BUILD):
                if later in results:
                    return later
            return _LABEL_COPIER
        return label

    def _database_left_ahead(self):
        """Whether an attempt began the database update and none restarted after.

        The job's mark (``cloud.job.no.return``), which the executor
        commits right before «Update changed modules» starts and which
        outlives the attempt; then no «Restart instance» line in the log
        after the last line of the update, as a restart puts the image
        the database was updated to in service. A log capped before
        both lines leaves the mark alone to tell.

        :rtype: bool
        """
        if not self.env["cloud.job.no.return"].sudo().search_count(
            [("job_id", "=", self.job.id)], limit=1,
        ):
            return False
        last = self.env["cloud.job.log.chunk"].sudo().search(
            [
                ("job_id", "=", self.job.id),
                ("content", "in", [
                    f"Running: {_LABEL_UPDATE}", f"Running: {_LABEL_RESTART}",
                ]),
            ],
            order="id desc", limit=1,
        )
        return last.content != f"Running: {_LABEL_RESTART}"

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
