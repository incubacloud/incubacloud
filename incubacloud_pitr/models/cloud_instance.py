"""Continuous recovery on ``cloud.instance``: settings, state and hooks.

The instance's own backup backend holds everything, under a prefix beside
the duplicity one (``<folder>/<name>-pitr``, never under it, so neither
the duplicity purge nor the archived-copy check ever sees it):

* ``<system id>/`` — wal-g's base copies and write-ahead log, one per
  PostgreSQL cluster (a move starts a new one);
* ``fs/`` — the filestore, encrypted by rclone (``current/`` and, per day,
  what was deleted that day).

The credentials live in one container, the ``pitr`` agent, never in the
database's: Odoo connects as a superuser, and an addon of the instance's
could read that container's environment with SQL. What the stack needs
comes through core's addon hooks; the jobs that act on the host are in
``pitr_executors``.
"""
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models.encrypted_char import EncryptedChar

from ._constants import (
    AGENT_SERVICE,
    ENV_FILE,
    OBJECTIVES,
    PGDATA,
    RCLONE_IMAGE,
    SPOOL_DIR,
    WALG_DIR,
    WALG_MOUNT,
    filestore_password,
    rclone_obscure,
)

_logger = logging.getLogger(__name__)

#: Job types of this module that change the stack, in flight one at a time.
PITR_STACK_JOBS = ("pitr_enable", "pitr_disable", "pitr_restore")
#: Every job type of this module.
PITR_JOBS = (*PITR_STACK_JOBS, "pitr_status", "pitr_base_backup")
#: A recovery target, as the panel and the API accept it (UTC).
_TARGET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?Z?$")
#: ``backup_retention`` (duplicity syntax) to days.
_RETENTION_RE = re.compile(r"^(\d+)([DWMY])$")
_RETENTION_DAYS = {"D": 1, "W": 7, "M": 30, "Y": 365}
#: Fields only a manager — or the panel itself — writes. Switching the
#: archive off or pointing it at the wrong cluster loses a customer's
#: history: a write is not a lesser way to do what the actions guard.
PITR_FIELDS = frozenset({
    "pitr_enabled", "pitr_objective", "pitr_stack_external", "pitr_key",
    "pitr_state", "pitr_cluster_id", "pitr_previous_clusters",
    "pitr_oldest_base_at", "pitr_last_base_at", "pitr_last_archived_at",
    "pitr_failed_count", "pitr_checked_at", "pitr_timelines",
})
#: How long a switch that failed waits before the next try.
_RETRY_AFTER = timedelta(days=1)
#: How long a base copy that failed waits: an hour, not the watch's ten
#: minutes.
_BASE_RETRY_AFTER = timedelta(hours=1)


def retention_days(retention):
    """Read a backup retention in duplicity's terms (``14D``, ``3M``) as days.

    :param str retention: what a backend or a plan keeps
    :return: days, or None for anything else (``0`` included)
    :rtype: int | None
    """
    match = _RETENTION_RE.match((retention or "").strip().upper())
    if not match or not int(match.group(1)):
        return None
    return int(match.group(1)) * _RETENTION_DAYS[match.group(2)]


class CloudInstance(models.Model):
    _inherit = "cloud.instance"

    pitr_enabled = fields.Boolean(
        string="Continuous Recovery",
        copy=False,
        help="Archive this production instance's database continuously "
        "and copy its filestore as it changes, so it can be restored to "
        "any minute of its backup window. Needs a backup backend. "
        "Switching it on or off rebuilds the instance and restarts its "
        "database once.",
    )
    # Not ``required``: a NOT NULL column on core's table would refuse the
    # rows core writes while this module is not loaded (an install of
    # another module, core's own tests at install).
    pitr_objective = fields.Selection(
        OBJECTIVES,
        string="Recovery Objective",
        default="5",
        help="The most the instance may lose: how often the database "
        "hands its log to the archive when it is quiet, and how often "
        "new files are copied.",
    )
    pitr_stack_external = fields.Boolean(
        string="Stack Configured Outside the Panel",
        copy=False,
        help="The instance's compose files are maintained outside the "
        "panel (the platform's own manager): the panel never rebuilds it "
        "for continuous recovery, and only runs the daily base copy and "
        "the watch.",
    )
    pitr_key = EncryptedChar(
        string="Recovery Encryption Key",
        groups="incubacloud.group_cloud_developer",
        copy=False,
        help="Encrypts the archive on the host before it is uploaded. "
        "Lost with the panel, the archive cannot be read.",
    )
    pitr_state = fields.Selection(
        [
            ("off", "Off"),
            ("enabling", "Switching on"),
            ("ok", "Archiving"),
            ("lagging", "Behind"),
            ("failed", "Failing"),
        ],
        string="Recovery State",
        default="off",
        copy=False,
        readonly=True,
    )
    pitr_cluster_id = fields.Char(
        string="Archived Cluster", copy=False, readonly=True,
        help="System identifier of the PostgreSQL cluster being archived.",
    )
    pitr_previous_clusters = fields.Json(
        string="Previous Clusters", copy=False, readonly=True,
        help="Clusters archived before this one (a move or a new deploy "
        "starts a new one), with the moment they stopped: "
        "[{'id': ..., 'until': ISO}]. Purged once out of the window.",
    )
    pitr_timelines = fields.Json(
        string="Restores to a Moment", copy=False, readonly=True,
        help="Each restore to a moment starts a new timeline of the "
        "cluster: [{'job', 'cluster', 'timeline', 'since', 'target'}]. "
        "A later restore follows them to pick its base copy.",
    )
    pitr_oldest_base_at = fields.Datetime(
        string="Recoverable From", copy=False, readonly=True,
    )
    pitr_last_base_at = fields.Datetime(
        string="Last Base Copy", copy=False, readonly=True,
    )
    pitr_last_archived_at = fields.Datetime(
        string="Last Archived", copy=False, readonly=True,
    )
    pitr_failed_count = fields.Integer(
        string="Archive Failures", copy=False, readonly=True,
    )
    pitr_checked_at = fields.Datetime(
        string="Recovery Last Checked", copy=False, readonly=True,
    )

    # ── What applies ─────────────────────────────────────────────────────

    def _pitr_wanted(self):
        """Whether continuous recovery is asked for on this instance.

        The field, in core. A layer that decides it otherwise (a plan)
        overrides this or writes the field.

        :rtype: bool
        """
        self.ensure_one()
        return bool(self.pitr_enabled)

    def _pitr_applicable(self):
        """Whether this instance can archive at all.

        Production only — a staging is a copy with no backups of its own
        and never archives — with an S3 backup backend.

        :rtype: bool
        """
        self.ensure_one()
        backend = self.effective_backup_backend
        return bool(
            self.environment == "production"
            and backend
            and backend.backend_type == "s3"
            and backend.s3_bucket
        )

    def _pitr_active(self):
        """Wanted and applicable: what the stack is rendered for.

        :rtype: bool
        """
        self.ensure_one()
        return self._pitr_wanted() and self._pitr_applicable()

    def _pitr_objective_minutes(self):
        """The recovery objective, in minutes.

        :rtype: int
        """
        self.ensure_one()
        return int(self.pitr_objective or "5")

    def _pitr_window_days(self):
        """How far back the instance can be restored: its backup retention.

        :return: days (7 when the retention cannot be read)
        :rtype: int
        """
        self.ensure_one()
        backend = self.effective_backup_backend
        return retention_days(backend.backup_retention if backend else "") or 7

    def _pitr_bucket_path(self):
        """Return ``(bucket, path)`` of the instance's recovery prefix.

        :rtype: tuple
        """
        self.ensure_one()
        backend = self.effective_backup_backend
        folder = (self.project_id.remote_folder if self.project_id else "") \
            or "default"
        parts = [(backend.s3_path or "").strip("/"), folder, f"{self.name}-pitr"]
        return backend.s3_bucket, "/".join(p for p in parts if p)

    def _pitr_prefix_base(self):
        """The wal-g prefix the clusters archive under (``s3://…``).

        :rtype: str
        """
        bucket, path = self._pitr_bucket_path()
        return f"s3://{bucket}/{path}"

    @api.model_create_multi
    def create(self, vals_list):
        """Every instance is born with its archive key.

        Created here rather than when continuous recovery is switched on:
        the executors that render the stack read it, and they must never
        write the job's transaction (core's executor write rule). Instances
        that predate this module get theirs from the install hook; a
        neutralized copy, whose key was cleared, from ``_pitr_ensure_key``.
        """
        for vals in vals_list:
            vals.setdefault("pitr_key", secrets.token_hex(32))
        return super().create(vals_list)

    def write(self, vals):
        """Only a manager, or the panel itself, changes continuous recovery.

        :raise AccessError: when another user writes one of its fields
        """
        if PITR_FIELDS.intersection(vals) and not self.env.su:
            self._pitr_check_manager()
        return super().write(vals)

    def _pitr_ensure_key(self):
        """Give the instance an archive key if it has none.

        A neutralized copy of production has its key cleared, so it never
        decrypts production's archive; switched on, it archives with a
        key of its own. Never called from an executor.
        """
        self.ensure_one()
        if not self.sudo().pitr_key:
            self.sudo().pitr_key = secrets.token_hex(32)

    def _pitr_key_value(self):
        """Return the instance's archive key.

        :rtype: str
        :raise UserError: when it has none (see ``_pitr_ensure_key``)
        """
        self.ensure_one()
        key = self.sudo().pitr_key
        if not key:
            raise UserError(_(
                "Instance '%s' has no continuous recovery key.", self.name,
            ))
        return key

    def _pitr_sync_hour(self):
        """The UTC hour of the daily base copy and exact filestore copy.

        Spread over 01:00-05:00 by instance, so a host's instances do not
        all upload at once.

        :rtype: int
        """
        self.ensure_one()
        digest = hashlib.sha256(str(self.id).encode()).digest()
        return 1 + digest[0] % 5

    # ── The stack (core hooks) ──────────────────────────────────────────

    def _pitr_renders(self):
        """Whether this module adds its pieces to the instance's stack.

        Not without a key: the agent's environment could not be written.
        """
        self.ensure_one()
        return (
            self._pitr_active()
            and not self.pitr_stack_external
            and bool(self.sudo().pitr_key)
        )

    def _extra_services(self):
        """Add the agent while continuous recovery is on.

        Also for a stack configured outside the panel, which declares the
        agent itself: listed, the health probe watches it.
        """
        services = super()._extra_services()
        if self._pitr_active():
            services = (*services, AGENT_SERVICE)
        return services

    def _compose_override_fragments(self):
        """The archive command's mount on ``db``; the agent.

        ``db`` gets the scripts and nothing secret, and starts archiving
        (``-c archive_mode=on``, which only a restart changes): switching
        on is then a reload, not a database restarting under whoever
        opens the instance. Until then it has no archive command and
        keeps its segments, which ``pitr_enable`` follows within minutes;
        ``pitr_disable`` lets them go until a rebuild drops the flag.

        The agent gets the credentials, the filestore, the data directory
        read-only (it reads the log the database marked ready, and the
        base copies), and the spool, the one place it writes the marks
        the archive command waits for. It idles until ``pitr_enable``
        installed its script, and is recreated then.

        :rtype: dict
        """
        fragments = super()._compose_override_fragments()
        if not self._pitr_renders():
            return fragments
        mount = f"./{WALG_DIR}:{WALG_MOUNT}:ro"
        script = f"{WALG_MOUNT}/pitr_agent.sh"
        loop = (
            f"[ -f {script} ] && exec sh {script} loop"
            f" {self._pitr_objective_minutes()} {self._pitr_sync_hour()}"
            " || exec sleep infinity"
        )
        fragments = dict(fragments)
        fragments["db"] = {"volumes": [mount], "command": ["-c", "archive_mode=on"]}
        fragments[AGENT_SERVICE] = {
            "image": RCLONE_IMAGE,
            "entrypoint": ["sh", "-c"],
            "command": [loop],
            "volumes": [
                "filestore:/src",
                f"db:{PGDATA}:ro",
                mount,
                f"./{SPOOL_DIR}:{WALG_MOUNT}/spool",
            ],
            # db-access.env: the password the agent connects with for a
            # base copy, the one Odoo uses.
            "env_file": [".docker/db-access.env", ENV_FILE],
            "restart": "unless-stopped",
        }
        return fragments

    def _extra_env_files(self):
        """The agent's environment, readable by its owner only.

        Written while the instance has an archive, also once switched off:
        deleting the instance purges the archive with the credentials of
        its last rebuild, as the duplicity purge does with backup.env.
        The README files create ``.walg/`` and its spool as the host user
        before the stack starts: a missing bind-mount source would be
        created by Docker, owned by root, and the install could not write
        to it.

        :rtype: dict
        """
        files = super()._extra_env_files()
        renders = self._pitr_renders()
        keeps_archive = (
            self.pitr_cluster_id and self._pitr_applicable()
            and not self.pitr_stack_external and self.sudo().pitr_key
        )
        if not (renders or keeps_archive):
            return files
        files = dict(files)
        files[ENV_FILE] = self._pitr_env()
        if renders:
            files[f"{WALG_DIR}/README.txt"] = (
                "Continuous recovery (incubacloud_pitr): wal-g, the scripts "
                "and the libraries they run with. Managed by the panel.\n"
            )
            files[f"{SPOOL_DIR}/README.txt"] = (
                "Written by the pitr agent: done/<segment> once a segment of "
                "the database's log is in the bucket; fs-ok, when the last "
                "filestore copy that worked started.\n"
            )
        return files

    def _pitr_env(self):
        """The agent's environment: wal-g, the database, rclone's remotes.

        ``pitrs3`` is the bucket, ``pitr`` the instance's prefix in it and
        ``pitrc`` an encrypted remote over its ``fs/``. wal-g archives each
        cluster under ``IC_PITR_PREFIX/<system id>``.

        :rtype: str
        """
        backend = self.effective_backup_backend.sudo()
        bucket, path = self._pitr_bucket_path()
        key = self._pitr_key_value()
        region = "auto" if backend.s3_endpoint_url else "us-east-1"
        lines = [
            f"IC_PITR_PREFIX={self._pitr_prefix_base()}",
            f"AWS_ACCESS_KEY_ID={backend.s3_access_key_id or ''}",
            f"AWS_SECRET_ACCESS_KEY={backend.s3_secret_access_key or ''}",
            f"AWS_REGION={region}",
            "AWS_S3_FORCE_PATH_STYLE=true",
            f"WALG_LIBSODIUM_KEY={key}",
            "WALG_LIBSODIUM_KEY_TRANSFORM=hex",
            "WALG_COMPRESSION_METHOD=brotli",
            "WALG_DELTA_MAX_STEPS=6",
            "WALG_PREVENT_WAL_OVERWRITE=true",
            "PGHOST=db",
            "PGPORT=5432",
            f"PGUSER={self.postgres_username or 'odoo'}",
            "PGDATABASE=postgres",
            f"PGDATA={PGDATA}",
            "RCLONE_CONFIG_PITRS3_TYPE=s3",
            "RCLONE_CONFIG_PITRS3_PROVIDER=Other",
            f"RCLONE_CONFIG_PITRS3_ACCESS_KEY_ID={backend.s3_access_key_id or ''}",
            "RCLONE_CONFIG_PITRS3_SECRET_ACCESS_KEY="
            f"{backend.s3_secret_access_key or ''}",
            f"RCLONE_CONFIG_PITRS3_REGION={region}",
            "RCLONE_CONFIG_PITRS3_NO_CHECK_BUCKET=true",
            "RCLONE_CONFIG_PITR_TYPE=alias",
            f"RCLONE_CONFIG_PITR_REMOTE=pitrs3:{bucket}/{path}",
            "RCLONE_CONFIG_PITRC_TYPE=crypt",
            "RCLONE_CONFIG_PITRC_REMOTE=pitr:fs",
            "RCLONE_CONFIG_PITRC_PASSWORD="
            f"{rclone_obscure(filestore_password(key))}",
        ]
        if backend.s3_endpoint_url:
            lines += [
                f"AWS_ENDPOINT={backend.s3_endpoint_url}",
                f"RCLONE_CONFIG_PITRS3_ENDPOINT={backend.s3_endpoint_url}",
            ]
        return "\n".join(lines) + "\n"

    def _teardown_pre_steps(self, inst_dir, keep_in_panel):
        """Empty the recovery prefix when the instance is deleted.

        Not when it is archived (``keep_in_panel``): its duplicity copy
        is what a revival restores, and the prefix stays with it until it
        is deleted for good. Purged with the stack up, like the duplicity
        prefix, so a purge that fails leaves the instance running; then
        once more after the agent and the database stopped, for what they
        uploaded in between.
        """
        steps = super()._teardown_pre_steps(inst_dir, keep_in_panel)
        if keep_in_panel or not (self.pitr_cluster_id or self.pitr_enabled):
            return steps
        # An ephemeral rclone with the agent's environment. rclone's 3 is
        # "directory not found": nothing was ever stored.
        purge = (
            f"rc=0; docker run --rm --env-file {ENV_FILE} {RCLONE_IMAGE}"
            " purge pitr: || rc=$?"
        )
        return steps + [
            (
                "Purge the continuous recovery archive",
                f"cd {inst_dir} && if [ -f {ENV_FILE} ]; then {purge};"
                ' [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ];'
                " else echo 'no recovery credentials here: the prefix is left"
                " in the bucket'; fi",
                {"stop_on_failure": True},
            ),
            (
                "Stop archiving and purge what arrived meanwhile",
                f"cd {inst_dir} && docker compose stop {AGENT_SERVICE} db"
                f" >/dev/null 2>&1; if [ -f {ENV_FILE} ]; then {purge};"
                ' [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ]'
                " || echo 'WARNING: the second purge failed; objects may be"
                " left in the prefix'; fi; true",
            ),
        ]

    def _after_restore_hook(self, kind):
        """A replaced database starts its archive again.

        A move lands on a new cluster, which has no archiving yet:
        switch it on there. Any other restore wrote a lot of history at
        once; a fresh base copy keeps a later restore from replaying it.
        A restore that is part of a move waits for its cutover. A restore
        to a moment (``pitr``) needs nothing here: its job has recorded
        the timeline it started and queued a full base copy of it
        (``PitrRestoreExecutor._restored``), and a second one would run
        beside it.
        """
        super()._after_restore_hook(kind)
        if kind == "pitr" or not self._pitr_active() or self.move_origin_host_id:
            return
        code = "pitr_enable" if kind == "move" else "pitr_base_backup"
        if self.pitr_stack_external:
            code = "pitr_base_backup"
        if code == "pitr_enable":
            self._pitr_ensure_key()
            self.sudo().pitr_state = "enabling"
        self.env["cloud.job"].sudo().enqueue(
            self.host_id.id, self.id, code, bypass_running_check=True,
        )

    def _after_stack_rendered(self):
        """Switch archiving on or off once a rebuild shipped the pieces.

        A plan applied, a rebuild after the field changed: the stack now
        has the agent and the archive command, or no longer has them, and
        only the database settings are left. Doing it here spares the
        second rebuild the watch would queue.
        """
        super()._after_stack_rendered()
        self._pitr_reconcile(after_rebuild=True)

    def _pitr_failed_lately(self, code, within=_RETRY_AFTER):
        """Whether a *code* job of this instance failed or was cancelled lately.

        :param timedelta within: how far back "lately" reaches
        :rtype: bool
        """
        self.ensure_one()
        return bool(self.env["cloud.job"].sudo().search_count([
            ("instance_id", "=", self.id),
            ("job_type_id.code", "=", code),
            ("state", "in", ("failed", "cancelled")),
            ("create_date", ">=", fields.Datetime.now() - within),
        ]))

    def _pitr_base_due(self):
        """Whether this instance's daily base copy is due now.

        The rule the watch queues the copy by. An archiving instance
        (still wanted and applicable) is due with no copy yet, with its
        last one older than 36 hours, or at its hour (``_pitr_sync_hour``)
        with it older than 20 hours; not within an hour of a copy that
        failed, so one that keeps failing is tried hourly rather than at
        every watch.

        :rtype: bool
        """
        self.ensure_one()
        if self.pitr_state not in ("ok", "lagging", "failed") or not self._pitr_active():
            return False
        if self._pitr_failed_lately("pitr_base_backup", _BASE_RETRY_AFTER):
            return False
        now = fields.Datetime.now()
        last = self.pitr_last_base_at
        return not last or last < now - timedelta(hours=36) or (
            self._pitr_sync_hour() == datetime.now(timezone.utc).hour
            and last < now - timedelta(hours=20)
        )

    def _pitr_others_busy(self):
        """Whether jobs of the instance besides the one running now are active.

        Counted from the panel's side: the job that called is one of them,
        background jobs (``_get_hidden_job_types``) are not.

        :rtype: bool
        """
        self.ensure_one()
        Job = self.env["cloud.job"].sudo()
        return Job.search_count([
            ("instance_id", "=", self.id),
            ("state", "in", Job._active_states),
            ("job_type_id.code", "not in", Job._get_hidden_job_types()),
        ]) > 1

    def _pitr_reconcile(self, after_rebuild=False):
        """Queue what brings the instance's archive in line with what is wanted.

        The one place that decides it, for the watch and after a rebuild.
        Wanted on and off: switch it on. Off and on: switch it off. Never
        during a move (the cutover does it on the target), never on a
        stack the panel does not render, never with a job of this module
        in flight, and not again within a day of a switch that failed: one
        that keeps failing must not rebuild the instance, or restart its
        database, every ten minutes.

        :param bool after_rebuild: a rebuild just rendered the stack as it
            is wanted now: only the switch itself is left to do
        :return: the job type queued first, or None
        :rtype: str | None
        """
        self.ensure_one()
        if self.move_origin_host_id or self.pitr_stack_external or not self.host_id:
            return None
        if self._pitr_job_in_flight(PITR_JOBS):
            return None
        if after_rebuild and self._pitr_others_busy():
            # More of the chain that rebuilt it is still to run (a revival
            # restores its backup next): switching restarts the database,
            # so it is left to the watch, which waits for the chain.
            return None
        state = self.pitr_state or "off"
        if state == "enabling":
            # Its chain ended without switching it on (a rebuild that
            # failed cancels the rest).
            state = "off"
        if self._pitr_active() and state == "off":
            if self._pitr_failed_lately("pitr_enable"):
                return None
            # Whether the stack has the pieces is decided before the key
            # exists: a rebuild without them comes first.
            rendered = after_rebuild and self._pitr_renders()
            self._pitr_ensure_key()
            self.sudo().pitr_state = "enabling"
            if rendered:
                self._pitr_enqueue(["pitr_enable"], head=after_rebuild)
            else:
                self._pitr_enqueue(
                    [self._pitr_rebuild_job_type(), "pitr_enable"],
                    head=after_rebuild,
                )
            return "pitr_enable"
        if not self._pitr_active() and state != "off":
            if self._pitr_failed_lately("pitr_disable"):
                return None
            if after_rebuild:
                self._pitr_enqueue(["pitr_disable"], head=after_rebuild)
            else:
                self._pitr_enqueue(
                    ["pitr_disable", self._pitr_rebuild_job_type()],
                    head=after_rebuild,
                )
            return "pitr_disable"
        if (self.pitr_state or "off") == "enabling":
            self.sudo().pitr_state = "off"
        return None

    # ── Actions ─────────────────────────────────────────────────────────

    def _pitr_check_manager(self):
        """Continuous recovery is switched and used by managers.

        Switching it rebuilds the instance and restarts its database, and
        a restore rewinds production: both above the backup operations a
        developer may run.

        :raise AccessError: below the manager role
        """
        self.env["cloud.security.mixin"]._check_cloud_group(
            "group_cloud_manager",
        )

    def _pitr_rebuild_job_type(self):
        """The rebuild that renders this instance's stack: the right one.

        A layer that rebuilds its instances with its own executor (a SaaS
        tenant) answers through ``cloud.github.event._rebuild_job_type``.

        :rtype: str
        """
        self.ensure_one()
        return (
            self.env["cloud.github.event"]._rebuild_job_type(self)
            or "rebuild_instance"
        )

    def _pitr_jobs_in_flight(self, codes=PITR_STACK_JOBS):
        """The jobs of *codes* queued or running for this instance.

        :rtype: ``cloud.job`` recordset
        """
        self.ensure_one()
        Job = self.env["cloud.job"].sudo()
        return Job.search([
            ("instance_id", "=", self.id),
            ("job_type_id.code", "in", list(codes)),
            ("state", "in", Job._active_states),
        ])

    def _pitr_job_in_flight(self, codes=PITR_STACK_JOBS):
        """Whether one of *codes* is queued or running for this instance."""
        return bool(self._pitr_jobs_in_flight(codes))

    def _pitr_refuse_busy(self):
        """Refuse a switch or a restore while one of the stack's jobs is active.

        Saying which: a job that only waits in the instance's queue has
        not started yet.

        :raise UserError: when a job of ``PITR_STACK_JOBS`` is queued or
            running
        """
        jobs = self._pitr_jobs_in_flight()
        if not jobs:
            return
        if all(job.state == "queued" for job in jobs):
            raise UserError(_(
                "A continuous recovery job is already waiting in this "
                "instance's queue.",
            ))
        raise UserError(_("A continuous recovery job is already running."))

    def _pitr_enqueue(self, codes, head=False):
        """Queue *codes* as one chain on this instance's host.

        With *head*, the job or chain goes to the head of the instance's
        queue and starts when the job that called for it ends: what a
        rebuild queues from ``_after_stack_rendered`` belongs to that
        rebuild. Without it (the watch's), it waits its turn.

        :param list codes: job type codes, in order
        :param bool head: whether it goes before what already waits
        :return: the first job's id
        """
        Job = self.env["cloud.job"].sudo()
        if len(codes) == 1:
            return Job.enqueue(
                self.host_id.id, self.id, codes[0], bypass_running_check=head,
            )
        return Job.enqueue_chain([
            {"host_id": self.host_id.id, "instance_id": self.id,
             "job_type_code": code}
            for code in codes
        ], bypass_running_check=head)[0]

    def action_pitr_enable(self, objective=None):
        """Switch continuous recovery on: rebuild, then start archiving.

        The rebuild renders the agent and the archive command into the
        stack; ``pitr_enable`` then installs wal-g, turns archiving on
        (one restart of the database) and takes the first base copy.

        :param str objective: optional new objective (minutes)
        :return: the first job's id
        :rtype: int
        """
        self.ensure_one()
        self._pitr_check_manager()
        self._pitr_refuse_external()
        if not self._pitr_applicable():
            raise UserError(_(
                "Continuous recovery needs a production instance with an "
                "S3 backup backend.",
            ))
        self._pitr_refuse_busy()
        if objective and str(objective) not in dict(OBJECTIVES):
            raise UserError(_("Unknown recovery objective: %s", objective))
        self._pitr_ensure_key()
        vals = {"pitr_enabled": True, "pitr_state": "enabling"}
        if objective:
            vals["pitr_objective"] = str(objective)
        self.sudo().write(vals)
        return self._pitr_enqueue([self._pitr_rebuild_job_type(), "pitr_enable"])

    def _pitr_refuse_external(self):
        """A stack configured outside the panel is switched by its runbook.

        Switching restarts the database with Odoo stopped around it; on
        the panel's own instance that is the process running the job.

        :raise UserError: for such a stack
        """
        if self.pitr_stack_external:
            raise UserError(_(
                "This instance's stack is configured outside the panel: "
                "switch continuous recovery with its runbook.",
            ))

    def action_pitr_disable(self):
        """Switch continuous recovery off: stop archiving, then rebuild.

        What is in the bucket stays until the instance is deleted.

        :return: the first job's id
        """
        self.ensure_one()
        self._pitr_check_manager()
        self._pitr_refuse_external()
        self._pitr_refuse_busy()
        self.sudo().write({"pitr_enabled": False})
        return self._pitr_enqueue(["pitr_disable", self._pitr_rebuild_job_type()])

    def action_pitr_set_objective(self, objective):
        """Change the recovery objective; applied with a rebuild.

        :param str objective: one of the allowed minutes
        """
        self.ensure_one()
        self._pitr_check_manager()
        if objective not in dict(OBJECTIVES):
            raise UserError(_("Unknown recovery objective: %s", objective))
        if objective == self.pitr_objective:
            return False
        self._pitr_refuse_external()
        self._pitr_refuse_busy()
        self.sudo().pitr_objective = objective
        if (self.pitr_state or "off") == "off":
            return False
        return self._pitr_enqueue([self._pitr_rebuild_job_type(), "pitr_enable"])

    def _pitr_window(self):
        """The moments this instance can be restored to, as UTC datetimes.

        From the oldest base copy of the current cluster to now: the log
        reaches the present within the objective.

        :return: ``(earliest, latest)`` or None when nothing is archived
        """
        self.ensure_one()
        if (self.pitr_state or "off") == "off" or not self.pitr_oldest_base_at:
            return None
        return self.pitr_oldest_base_at, fields.Datetime.now()

    def action_pitr_restore(self, target):
        """Restore the instance to *target* (UTC): its database and files.

        A duplicity snapshot of the present is taken first, when the
        instance has one, as before any restore. The restore itself
        first archives what the database has not handed over yet, so the
        present stays in the archive too.

        :param str target: ``YYYY-MM-DD HH:MM[:SS]`` in UTC
        :return: the first job's id
        """
        self.ensure_one()
        self._pitr_check_manager()
        if self.pitr_stack_external:
            raise UserError(_(
                "This instance's stack is configured outside the panel: "
                "restore it with its runbook.",
            ))
        when = self._pitr_parse_target(target)
        window = self._pitr_window()
        if not window or not (window[0] <= when <= window[1]):
            raise UserError(_(
                "%(when)s is outside the window this instance can be "
                "restored to.", when=when,
            ))
        self._pitr_refuse_busy()
        steps = []
        if "backup" in self.expected_services():
            steps.append({
                "host_id": self.host_id.id, "instance_id": self.id,
                "job_type_code": "backup_create",
            })
        steps.append({
            "host_id": self.host_id.id, "instance_id": self.id,
            "job_type_code": "pitr_restore",
            "payload": {"target": fields.Datetime.to_string(when)},
        })
        return self.env["cloud.job"].sudo().enqueue_chain(steps)[0]

    @api.model
    def _pitr_parse_target(self, target):
        """Read a recovery target (UTC) or refuse it.

        :param str target: ``YYYY-MM-DD HH:MM[:SS]``, optionally with
            ``T`` and a trailing ``Z``
        :rtype: datetime
        :raise UserError: on anything else
        """
        raw = (target or "").strip()
        if not _TARGET_RE.match(raw):
            raise UserError(_("Not a date and time: %s", raw))
        raw = raw.replace("T", " ").rstrip("Z")
        if len(raw) == 16:
            raw += ":00"
        return fields.Datetime.to_datetime(raw)

    def _pitr_record_restore(self, result):
        """Remember the timeline a restore to a moment started.

        Called from the job that followed it, or from the watch when the
        restore outlived its jobs. Each restore is recorded once, and
        whichever records it follows it up (``_follow_up_restore``).

        :param dict result: the restore's ``job``, ``timeline`` (8 hex
            digits), ``promoted_at`` (epoch) and ``target``
        :return: ``True`` when this call recorded it, ``False`` when it
            was recorded before, ``None`` when *result* says too little to
            record it (``pitr.sh`` reads the new timeline after the
            promotion, and that read may fail)
        :rtype: bool or None
        """
        self.ensure_one()
        job = str(result.get("job") or "")
        timeline = result.get("timeline") or ""
        promoted = int(result.get("promoted_at") or 0)
        if not (job and re.fullmatch(r"[0-9A-F]{8}", timeline) and promoted):
            return None
        entries = list(self.pitr_timelines or [])
        if any(str(e.get("job")) == job for e in entries):
            return False
        entries.append({
            "job": job,
            "cluster": self.pitr_cluster_id,
            "timeline": timeline,
            "since": fields.Datetime.to_string(
                datetime.fromtimestamp(promoted, tz=timezone.utc).replace(tzinfo=None),
            ),
            "target": (result.get("target") or "").replace("T", " ")[:19],
        })
        self.sudo().pitr_timelines = entries[-50:]
        return True

    def _pitr_status_vals(self):
        """What the panel shows about this instance's continuous recovery.

        :rtype: dict
        """
        self.ensure_one()
        window = self._pitr_window()
        return {
            "available": self._pitr_applicable(),
            "enabled": bool(self.pitr_enabled),
            "external": bool(self.pitr_stack_external),
            "state": self.pitr_state or "off",
            "objective": self.pitr_objective,
            "objectives": [k for k, _label in OBJECTIVES],
            "window_days": self._pitr_window_days(),
            "from": fields.Datetime.to_string(window[0]) if window else False,
            "to": fields.Datetime.to_string(window[1]) if window else False,
            "last_archived": fields.Datetime.to_string(
                self.pitr_last_archived_at,
            ) if self.pitr_last_archived_at else False,
            "last_base": fields.Datetime.to_string(
                self.pitr_last_base_at,
            ) if self.pitr_last_base_at else False,
            # When the watch last read the host: "last archived" is as
            # fresh as this, and the card says so rather than let a
            # ten-minute-old reading pass for archiving behind.
            "checked": fields.Datetime.to_string(
                self.pitr_checked_at,
            ) if self.pitr_checked_at else False,
            "busy": self._pitr_job_in_flight(),
        }

    # ── Crons ───────────────────────────────────────────────────────────

    @api.model
    def _pitr_candidates(self):
        """Running production instances continuous recovery concerns.

        Not a stopped one (nothing archives, a reading would fail every
        tick), nor one in the middle of a move.
        """
        return self.search(
            [
                ("deployed", "=", True),
                ("environment", "=", "production"),
                ("host_id", "!=", False),
                ("running", "=", True),
                ("move_origin_host_id", "=", False),
                "|", ("pitr_enabled", "=", True),
                ("pitr_state", "not in", (False, "off")),
            ]
            + self._periodic_maintenance_domain()
        )

    @api.model
    def _cron_pitr_watch(self):
        """Reconcile every instance concerned; read the archiving ones.

        Every ten minutes. An archiving instance whose base copy is due
        (``_pitr_base_due``) gets the copy instead of the reading: this
        is the only place the daily copy is queued, and a reading queued
        here would hold it back, as no instance gets a second job of
        this module while one is in flight. One instance that fails
        does not stop the others: each runs in its own savepoint.

        :return: how many instances got a job
        :rtype: int
        """
        Job = self.env["cloud.job"].sudo()
        queued = 0
        for inst in self._pitr_candidates():
            try:
                with self.env.cr.savepoint():
                    if inst.pitr_stack_external:
                        # Switched by its runbook; the panel only watches.
                        code = None
                    else:
                        code = inst._pitr_reconcile()
                    if not code and (inst.pitr_state or "off") != "off" \
                            and not inst._pitr_job_in_flight(PITR_JOBS):
                        code = (
                            "pitr_base_backup" if inst._pitr_base_due()
                            else "pitr_status"
                        )
                        Job.enqueue(inst.host_id.id, inst.id, code)
                    if code:
                        queued += 1
            except Exception as exc:  # noqa: BLE001 — one must not stop all
                _logger.warning(
                    "Continuous recovery watch skipped %s: %s", inst.name, exc,
                )
        return queued
