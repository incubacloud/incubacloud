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
from datetime import datetime, timezone

from odoo import fields

from .abstract_executor import sql_escape_literal
from .deploy_instance_executor import DeployInstanceExecutor


class RebuildInstanceExecutor(DeployInstanceExecutor):
    """Rebuild a running doodba instance using copier update."""

    _job_type = "rebuild_instance"

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("rebuild_instance job has no instance_id")
        await self._preflight_addon_check()
        self._sys(f"Preparing rebuild for '{inst.name}'...")
        await self._upload_copier_files(transport)

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
                "Update with copier",
                self.run_script(
                    "rebuild.sh",
                    ["copier-update", d, tmp_answers,
                     self._copier_template()[1]],
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

        # 3c. Cap the backup container hostname at 64 bytes (see the deploy
        #     executor for the full rationale). ``copier update`` regenerates
        #     common.yaml, so this runs on every rebuild too.
        if self._backup_enabled() and inst.environment == 'production':
            cmds.append((
                "Cap backup hostname",
                self.run_script("deploy.sh", ["cap-backup-hostname", d, name]),
            ))

        cmds += [
            # 4. Overwrite backup.env with ours (adds AWS_ENDPOINT_URL).
            (
                "Write backup.env",
                f"f={self._tmp('backup.env')};"
                f" [ -f \"$f\" ] && mv \"$f\" {d}/.docker/backup.env || true",
            ),
            # 4b. Ensure .docker/incubacloud.env exists (generate once,
            #     never overwrite — survives rebuilds).
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
            # 4d. Write docker-compose.override.yml with resource limits.
            (
                "Write resource limits",
                f"f={self._tmp('override.yml')};"
                f" [ -f \"$f\" ] &&"
                f" mv \"$f\" {d}/docker-compose.override.yml || true",
            ),
            # 5. Overwrite repos.yaml and addons.yaml.
            ("Write addons.yaml", f"mv {tmp_addons} {src}/addons.yaml"),
            ("Write repos.yaml", f"mv {tmp_repos} {src}/repos.yaml"),
            # 6. Copy incubacloud_connect.
            (
                "Install incubacloud_connect",
                f"mkdir -p {src}/private"
                f" && cp -r /tmp/.ic-modules-{name}/incubacloud_connect"
                f" {src}/private/"
                f" && rm -rf /tmp/.ic-modules-{name}",
            ),
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
            # 8. Remove the temp answers file.
            ("Remove temp files", f"rm -f {tmp_answers}"),
            # 9. Capture services list for on_success (parsed from stdout).
            ("List services", f"cd {d} && docker compose config --services"),
            # 10. Rebuild Odoo image.
            #     Smart rebuild: full (--pull --no-cache) when config that
            #     affects the image has changed; cached otherwise.
            (
                "Rebuild Odoo image",
                (
                    f"cd {d} && docker compose build"
                    f" --pull --no-cache odoo"
                ) if needs_full_rebuild else (
                    f"cd {d} && docker compose build odoo"
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
            #     modules that actually changed.
            (
                "Update changed modules",
                f"cd {d} && docker compose run --rm odoo"
                f" click-odoo-update"
                f" --database {inst.postgres_dbname or 'prod'}",
                {"stop_on_failure": True},
            ),
            ]
        cmds += [
            # 12b. Ensure incubacloud_connect is installed (restored DBs
            #      may not have it; click-odoo-update only updates, not installs).
            (
                "Ensure incubacloud_connect",
                f"cd {d} && docker compose run --rm odoo"
                f" odoo -d {inst.postgres_dbname or 'prod'}"
                f" -i incubacloud_connect"
                f" --stop-after-init --no-http",
            ),
            # 13. Restart all services with the new image.
            #     --remove-orphans drops containers no longer in the compose
            #     file (e.g. backup or smtp when the operator removes the
            #     corresponding service from the instance config) so they
            #     don't linger.
            (
                "Restart instance",
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
        # "List services" exit_status != 0 only means compose isn't configured;
        # don't treat it as a fatal error — the rebuild itself may have worked.
        ignored = {"List services"}
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get("exit_status", 1) != 0 and label not in ignored
        ]

    async def on_success(self, results):
        inst = self._inst()
        write_vals = {"status": "ok"}
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
        write_vals['applied_config_hash'] = inst._config_snapshot_hash()
        inst.write(write_vals)
        self._sys(
            f"✓ '{inst.name}' rebuilt and restarted"
            f" with updated configuration."
        )
        self._enqueue_coalesced_rebuild_if_pending(inst)

    def _enqueue_coalesced_rebuild_if_pending(self, inst):
        """Fold queued pushes into a follow-up rebuild after a successful one.

        Pending pushes accumulated by ``cloud.github.event`` while this
        rebuild was running (or within the cooldown window) are
        consumed here in the post-success cursor: their payloads ride
        along on a new ``rebuild_instance`` job so every skipped push
        remains visible end-to-end, and the unlink + new enqueue commit
        atomically with the success write.
        """
        if not inst or not inst.host_id:
            return
        pending = inst.pending_push_ids.sorted('create_date')
        if not pending:
            return
        coalesced = [p._to_payload() for p in pending]
        head = coalesced[-1]
        payload = {
            'trigger': 'coalesced',
            'push_repo': head['push_repo'],
            'push_branch': head['push_branch'],
            'push_sha': head['push_sha'],
            'push_message': head['push_message'],
            'push_by': head['push_by'],
            'coalesced_pushes': coalesced,
        }
        try:
            self.env['cloud.job'].enqueue(
                inst.host_id.id, inst.id, 'rebuild_instance',
                payload=payload,
                # This runs from on_success, so THIS rebuild is still
                # 'started' on the very same instance: the active-job
                # guard would match it and refuse to queue the follow-up,
                # leaving the coalesced pushes stranded forever.
                bypass_running_check=True,
            )
            inst.write({'last_auto_rebuild': fields.Datetime.now()})
            pending.unlink()
            self._sys(
                f"↻ Enqueued coalesced rebuild carrying"
                f" {len(coalesced)} queued push(es)."
            )
        except Exception:
            # Leave the pending rows intact so the operator can see what
            # was queued; the next webhook or manual rebuild will pick
            # them up.
            self._sys(
                "⚠ Failed to enqueue coalesced rebuild — pending pushes"
                " preserved for the next attempt."
            )

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
