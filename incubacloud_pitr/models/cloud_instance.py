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
import shlex
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models import _odoo_versions
from odoo.addons.incubacloud.models._backup_dst import compose_env_line
from odoo.addons.incubacloud.models.encrypted_char import EncryptedChar

from ._constants import (
    AGENT_SERVICE,
    ALERT_MODULES,
    ALERT_PREFIX_CHANGED,
    ALERT_PREFIX_LEFT,
    ALERT_PURGE_FAILED,
    ENV_FILE,
    OBJECTIVES,
    PGDATA,
    PREFIX_LEFT_MARK,
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
PITR_JOBS = (*PITR_STACK_JOBS, "pitr_status", "pitr_base_backup", "pitr_purge")
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
    "pitr_archived_prefix", "pitr_off_at", "pitr_purge_at",
    "pitr_rendered_prefix", "pitr_rendered_backend_id",
})
#: Label of the delete's first purge of the recovery prefix.
PURGE_LABEL = "Purge the continuous recovery archive"
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
    pitr_archived_prefix = fields.Char(
        string="Archived Recovery Prefix", copy=False, readonly=True,
        help="Where the recovery archive was when the instance was "
        "archived (s3://bucket/path), emptied when it is deleted for "
        "good. Frozen as the duplicity path is: the computed one follows "
        "the project, which may change or go meanwhile.",
    )
    pitr_off_at = fields.Datetime(
        string="Recovery Switched Off", copy=False, readonly=True,
        help="When the instance stopped archiving with an archive in the "
        "bucket. What was archived before it is emptied on the purge date.",
    )
    pitr_purge_at = fields.Datetime(
        string="Recovery Archive Purge", copy=False, readonly=True,
        help="When the archive of an instance switched off is emptied: the "
        "moment it was switched off plus the backup window it had then. "
        "Switching it on again before then keeps the archive.",
    )
    pitr_rendered_prefix = fields.Char(
        string="Rendered Recovery Prefix", copy=False, readonly=True,
        help="The prefix the instance's server was last given to archive "
        "to (s3://bucket/path), kept by every rebuild that writes the "
        "recovery credentials.",
    )
    pitr_rendered_backend_id = fields.Many2one(
        "cloud.backup.backend", string="Rendered Recovery Destination",
        copy=False, readonly=True, ondelete="set null",
        help="The backup destination of the rendered recovery prefix.",
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
        and never archives — with an S3 backup backend, on a PostgreSQL
        ``pitr.sh`` can recover (``_pitr_postgres_supported``).

        :rtype: bool
        """
        self.ensure_one()
        backend = self.effective_backup_backend
        return bool(
            self.environment == "production"
            and backend
            and backend.backend_type == "s3"
            and backend.s3_bucket
            and self._pitr_postgres_supported()
        )

    def _pitr_postgres_supported(self):
        """Whether this instance's PostgreSQL can be recovered by ``pitr.sh``.

        It writes ``recovery.signal``, from 12 on; before, recovery was
        another file and other settings. No series runs on an older one
        (``_odoo_versions``): this only holds back a record stored on a
        PostgreSQL that the table no longer offers.

        :rtype: bool
        """
        self.ensure_one()
        majors = _odoo_versions.POSTGRES_MAJORS
        return majors.index(self.postgres_version or "17") >= majors.index("12")

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

        Beside the duplicity path, with its suffix
        (``backup_path_suffix``): ``<folder>/<name><suffix>-pitr``.

        :rtype: tuple
        """
        self.ensure_one()
        backend = self.effective_backup_backend
        parts = [
            (backend.s3_path or "").strip("/"), self._backup_folder(),
            f"{self.name}{self.backup_path_suffix or ''}-pitr",
        ]
        return backend.s3_bucket, "/".join(p for p in parts if p)

    def _backup_path_tails(self, suffix):
        """Core's endings and the recovery prefix's, ``…-pitr``.

        :param str suffix: a ``backup_path_suffix``, ``""`` for none
        :rtype: list
        """
        tails = list(super()._backup_path_tails(suffix))
        return tails + [f"/{self._backup_folder()}/{self.name}{suffix}-pitr"]

    def _pitr_prefix_base(self):
        """The wal-g prefix the clusters archive under (``s3://…``).

        :rtype: str
        """
        bucket, path = self._pitr_bucket_path()
        return f"s3://{bucket}/{path}"

    def _pitr_had_archive(self):
        """Whether the instance may have something under its recovery prefix.

        Archiving on now, or a cluster recorded: switching off keeps the
        cluster and leaves the archive in the bucket (``pitr_disable``).
        What the delete's purges go by.

        :rtype: bool
        """
        self.ensure_one()
        return bool(self.pitr_cluster_id or self.pitr_enabled)

    def _freeze_backup_dst(self):
        """Freeze the recovery prefix beside the duplicity path.

        Core freezes the chain's path when the instance is archived
        because the computed one follows the project; the recovery prefix
        follows it too (``_pitr_bucket_path``), and an archived instance
        whose project changed or went would have its archive purged at a
        prefix that is not its own, or not at all. The prefix its server
        was last given (``pitr_rendered_prefix``) when there is one: that
        is where it archived. Only for an instance that had an archive, on
        a destination with a bucket; a frozen one is kept.
        """
        super()._freeze_backup_dst()
        if self.pitr_archived_prefix or not self._pitr_had_archive():
            return
        if self.pitr_rendered_prefix:
            self.sudo().write({"pitr_archived_prefix": self.pitr_rendered_prefix})
            return
        bucket, path = self._pitr_bucket_path()
        if not (bucket and path):
            return
        self.sudo().write({"pitr_archived_prefix": f"s3://{bucket}/{path}"})

    def _pitr_prefix_from_chain(self):
        """The recovery prefix of an archived instance, read off its chain.

        For an instance archived before the prefix was frozen
        (``pitr_archived_prefix``): what ``_pitr_bucket_path`` gave when it
        was archived is its frozen chain path (``custom_backup_dst``) with
        ``-pitr`` after it, the same destination path, folder and name
        (with the name's suffix, ``backup_path_suffix``).
        The one computed now follows the project: renamed, it names an
        empty folder; gone, the shared ``default`` one, where another
        instance of the same name may archive. Only a chain path that
        ends in the instance's name, on S3, says it; one imported by hand
        may say anything else.

        :return: ``s3://bucket/path``, or ``""`` when the chain path does
            not say it
        :rtype: str
        """
        self.ensure_one()
        scheme = "boto3+s3://"
        dst = (self.custom_backup_dst or "").rstrip("/")
        if not (self.name and dst.startswith(scheme)):
            return ""
        rest = dst[len(scheme):]
        bucket, _sep, path = rest.partition("/")
        own = f"/{self.name}{self.backup_path_suffix or ''}"
        if not (bucket and path) or not rest.endswith(own):
            return ""
        return f"s3://{rest}-pitr"

    def _archived_copy_extra_prefixes(self):
        """Delete an archived instance's recovery archive with its chain.

        The prefix frozen when it was archived or, for one archived
        before the prefix was frozen, the one its chain path says
        (``_pitr_prefix_from_chain``); never the one computed now. The
        purge runs it with the cutoff of the delete (``purge_cutoff_at``),
        so a successor of the same name keeps what it wrote since.

        :return: core's prefixes and the recovery one
        :rtype: list
        """
        prefixes = list(super()._archived_copy_extra_prefixes())
        prefix = self.pitr_archived_prefix
        if not prefix and self._pitr_had_archive():
            prefix = self._pitr_prefix_from_chain()
        if prefix:
            prefixes.append(prefix)
        return prefixes

    def _backup_paths_held(self):
        """Core's paths and the recovery archive's prefixes.

        The prefix frozen when the instance was archived, the one its
        server was last given and, while it may have an archive, the one
        computed now: another record's purge must reach none of them
        (``_backup_prefix_conflict``).

        :rtype: list
        """
        paths = list(super()._backup_paths_held())
        paths += [
            p for p in (self.pitr_archived_prefix, self.pitr_rendered_prefix)
            if p
        ]
        if self._pitr_had_archive():
            bucket, path = self._pitr_bucket_path()
            if bucket and path:
                paths.append(f"s3://{bucket}/{path}")
        return paths

    def delete_archived(self, payload=None):
        """Say so when the recovery archive cannot go with the copy.

        An instance archived before the prefix was frozen, whose chain
        path does not say where its archive is
        (``_pitr_prefix_from_chain``): nothing computed is purged, as it
        may be another instance's, and a person is told where to look.
        Only for one that recorded a cluster: one switched on that never
        archived has nothing there. Read before core's, which may unlink
        the record.

        :param payload: as for core's ``delete_archived``
        :return: as for core's ``delete_archived``
        """
        self.ensure_one()
        unknown = (
            not self.active and self.pitr_cluster_id
            and not self.pitr_archived_prefix
            and not self._pitr_prefix_from_chain()
        )
        message = key = backend = None
        if unknown:
            key = self._pitr_unknown_prefix_key()
            message = (
                f"The continuous recovery archive of '{self.name}' is not "
                "emptied with its archived copy: the instance was archived "
                "before its recovery prefix was kept, and its backup path "
                f"({self.custom_backup_dst or 'none'}) does not say where "
                f"the archive is. Look for a prefix ending in "
                f"{self.name}{self.backup_path_suffix or ''}-pitr on its "
                "backup destination, empty it by hand (RB-23 §5), then "
                "resolve this alert."
            )
            backend = self.effective_backup_backend
        result = super().delete_archived(payload=payload)
        if unknown:
            self._pitr_alert_prefix_left(message, key, backend)
        return result

    def _pitr_unknown_prefix_key(self):
        """The alert key of a recovery archive whose prefix is not known.

        :rtype: str
        """
        self.ensure_one()
        return f"{self.name}-pitr (instance {self.id})"

    def _pitr_alert_prefix_left(self, message, key, backend, job=None):
        """Raise ``pitr_prefix_left`` for a recovery prefix left to a person.

        On no host and no instance: an alert hangs on them with a cascade,
        and the instance is going and its server may go after it. Keyed
        by the prefix (or ``_pitr_unknown_prefix_key``). To the service
        provider when the destination is managed
        (``managed_reservation_id``): only whoever runs that bucket holds
        its credentials. Reads nothing off the record, which may be gone
        by then.

        :param str message: what was left and what to do
        :param str key: the alert's ``dedup_key``
        :param backend: the ``cloud.backup.backend`` the archive is on,
            possibly empty
        :param job: the ``cloud.job`` that left it, if any
        """
        managed = bool(backend and backend.sudo().managed_reservation_id)
        self.env["cloud.alert"].sudo().raise_alert(
            ALERT_PREFIX_LEFT, message, level="warning", job=job, key=key,
            audience="provider" if managed else "fleet",
        )
        # Not the instance's name: ``delete_archived`` may have unlinked it.
        _logger.warning("recovery archive left to a person: %s", key)

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
        if not self._pitr_writes_env():
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

    def _pitr_writes_env(self):
        """Whether the stack gets the agent's environment (``pitr.env``).

        While it archives, and once switched off while the instance still
        has an archive (a cluster recorded): the delete's purge reads it.

        :rtype: bool
        """
        self.ensure_one()
        if self._pitr_renders():
            return True
        return bool(
            self.pitr_cluster_id and self._pitr_applicable()
            and not self.pitr_stack_external and self.sudo().pitr_key
        )

    def _pitr_env(self):
        """The agent's environment: wal-g, the database, rclone's remotes.

        ``pitrs3`` is the bucket, ``pitr`` the instance's prefix in it and
        ``pitrc`` an encrypted remote over its ``fs/``. wal-g archives each
        cluster under ``IC_PITR_PREFIX/<system id>``. Both sign with the
        destination's region (``s3_region``).

        :rtype: str
        :raises ValueError: when compose would not read a value as it is
            written (``compose_env_line``), before the file reaches the
            host
        """
        backend = self.effective_backup_backend.sudo()
        bucket, path = self._pitr_bucket_path()
        key = self._pitr_key_value()
        # The destination's region; without one, R2's ``auto`` behind an
        # endpoint and AWS's default otherwise. Providers such as OVH
        # refuse a request signed for another region.
        region = backend.s3_region or (
            "auto" if backend.s3_endpoint_url else "us-east-1"
        )
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
        # Each line through ``compose_env_line``: the agent reads this file
        # as its ``env_file``, where a line break, or a value that opens
        # with a quote, would start a variable of its own, such as another
        # ``IC_PITR_PREFIX``. The names hold no ``=``.
        return "\n".join(
            compose_env_line(*line.split("=", 1)) for line in lines
        ) + "\n"

    def _teardown_pre_steps(self, inst_dir, keep_in_panel):
        """Empty the recovery prefix when the instance is deleted.

        Not when it is archived (``keep_in_panel``): its duplicity copy
        is what a revival restores, and the prefix stays with it until it
        is deleted for good. Purged with the stack up, like the duplicity
        prefix, so a purge that fails leaves the instance running; then
        once more after the agent and the database stopped, for what they
        uploaded in between. Either one failing stops the delete, as the
        duplicity purge does: what it left in the prefix would otherwise
        outlive the instance, with nothing left that knows where it is.
        The second one, or the stop before it, starts again what it
        stopped, so the instance is left running for the delete to be
        run again; a compose it cannot read stops it before it touches
        anything.

        When an archived instance of the same name keeps its archive in
        the same prefix (``_backup_purge_since``), only what was stored
        since this instance was created is deleted, by the storage's own
        dates: the archived one's stays for its own purge.
        """
        steps = super()._teardown_pre_steps(inst_dir, keep_in_panel)
        if keep_in_panel or not self._pitr_had_archive():
            return steps
        _backend, prefix = self._pitr_purge_target()
        since = self._backup_purge_since(prefix) if prefix else ""
        what = (
            "delete --use-server-modtime"
            f" --max-age {shlex.quote(since)} pitr:" if since else "purge pitr:"
        )
        # An ephemeral rclone with the agent's environment. rclone's 3 is
        # "directory not found": nothing was ever stored.
        purge = (
            f"rc=0; docker run --rm --env-file {ENV_FILE} {RCLONE_IMAGE}"
            f" {what} || rc=$?"
        )
        return steps + [
            (
                PURGE_LABEL,
                # No credentials on the host: the delete goes on, and
                # ``_teardown_pre_steps_left`` reads the mark and alerts.
                f"cd {inst_dir} && if [ -f {ENV_FILE} ]; then {purge};"
                ' [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ];'
                f" else echo '{PREFIX_LEFT_MARK} no recovery credentials"
                " here: the prefix is left in the bucket'; fi",
                {"stop_on_failure": True},
            ),
            (
                "Stop archiving and purge what arrived meanwhile",
                # Archiving switched off keeps the credentials for this
                # purge but not the agent, and compose refuses to stop a
                # service it does not declare: only the database then.
                f"cd {inst_dir} && services=$(docker compose config --services)"
                " && set -- db && if printf '%s\\n' \"$services\" | grep -qx"
                f" {AGENT_SERVICE}; then set -- {AGENT_SERVICE} db; fi"
                ' && if ! { docker compose stop "$@" >/dev/null &&'
                f" {{ [ ! -f {ENV_FILE} ] || {{ {purge};"
                ' [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ]; }; }; }; then'
                " echo 'ERROR: the archive could not be emptied once"
                " archiving stopped: the instance is not deleted' >&2;"
                ' if docker compose start "$@"; then'
                ' echo "$* running again: the delete can be run again";'
                ' else echo "ERROR: $* could not be started again" >&2; fi;'
                " false; fi",
                {"stop_on_failure": True},
            ),
        ]

    def _teardown_purge_prefixes(self, keep_in_panel):
        """The recovery prefix the delete's purge empties, when it runs one.

        The prefix its server was last given, or the one computed now
        (``_pitr_purge_target``): what the host's ``pitr.env`` names.

        :param bool keep_in_panel: the record stays as an archived draft
        :return: core's prefixes and the recovery one
        :rtype: list
        """
        prefixes = list(super()._teardown_purge_prefixes(keep_in_panel))
        if keep_in_panel or not self._pitr_had_archive():
            return prefixes
        _backend, prefix = self._pitr_purge_target()
        if prefix:
            prefixes.append(prefix)
        return prefixes

    def _teardown_pre_steps_left(self, results, job):
        """Alert when the delete left the recovery prefix in the bucket.

        The purge finds no ``pitr.env`` on the host (it was removed by
        hand, or never written) and lets the delete go on: refusing would
        leave an instance nobody can delete until a rebuild writes the
        file again. What it left must reach a person: an alert naming the
        prefix to empty (RB-23 §5, ``_pitr_alert_prefix_left``) and a line
        in the job's log. Only for an instance that recorded a cluster:
        one switched on whose start failed before its rebuild never wrote
        the file nor archived anything. With no destination now, the
        prefix is not known, and the alert says so.

        :param dict results: the delete's steps, as core passes them
        :param job: the delete's ``cloud.job``
        :return: core's messages and this one
        :rtype: list
        """
        left = list(super()._teardown_pre_steps_left(results, job))
        stdout = ((results or {}).get(PURGE_LABEL) or {}).get("stdout") or ""
        if PREFIX_LEFT_MARK not in stdout or not self.pitr_cluster_id:
            return left
        bucket, path = self._pitr_bucket_path()
        host = self.host_id.name or "?"
        if bucket and path:
            key = f"s3://{bucket}/{path}"
            message = (
                f"The continuous recovery archive of '{self.name}' was left "
                f"in the bucket at {key}: its host '{host}' had no recovery "
                f"credentials ({ENV_FILE}) when the instance was deleted. "
                "Empty that prefix by hand (RB-23 §5), then resolve this "
                "alert."
            )
        else:
            key = self._pitr_unknown_prefix_key()
            message = (
                f"The continuous recovery archive of '{self.name}' was left "
                f"behind: its host '{host}' had no recovery credentials "
                f"({ENV_FILE}) when the instance was deleted, and the "
                "instance had no backup destination by then, so where the "
                "archive is is unknown. Look for a prefix ending in "
                f"{self.name}{self.backup_path_suffix or ''}-pitr on the "
                "destination it archived to, empty it by hand (RB-23 §5), "
                "then resolve this alert."
            )
        self._pitr_alert_prefix_left(
            message, key, self.effective_backup_backend, job=job,
        )
        return [*left, message]

    def _after_restore_hook(self, kind):
        """A replaced database starts its archive again.

        A move lands on a new cluster, which has no archiving yet:
        switch it on there. Any other restore wrote a lot of history at
        once; a fresh base copy keeps a later restore from replaying it.
        A restore that is part of a move waits for its cutover. A restore
        to a moment (``pitr``) needs nothing here: its job has recorded
        the timeline it started and queued a full base copy of it
        (``PitrRestoreExecutor._restored``), and a second one would run
        beside it. Nor beside a rebuild that follows the restore: it
        restarts the database under the copy, and the watch queues the copy
        when it ends (``_pitr_base_due``).
        """
        super()._after_restore_hook(kind)
        if kind == "pitr" or not self._pitr_active() or self.move_origin_host_id:
            return
        if self._restore_is_followed_by_a_rebuild():
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

    def revive(self, host_id=None):
        """An archived instance comes back on a new cluster, with no archiving.

        Its stack was removed with the deletion, wal-g included, so the state
        it was deleted with says nothing: a base copy queued on it failed on
        the first panel brought back (QA, 1-nov-2026). Off, the deploy switches
        it on as it does for any new instance (``_after_stack_rendered``).

        :param host_id: as for core's ``revive``
        :return: as for core's ``revive``
        """
        result = super().revive(host_id=host_id)
        # Live again, its archive is the computed prefix's: one frozen
        # now would be the one the next archiving keeps.
        self.sudo().write({"pitr_state": "off", "pitr_archived_prefix": False})
        return result

    def _after_stack_rendered(self):
        """Switch archiving on or off once a rebuild shipped the pieces.

        A plan applied, a rebuild after the field changed: the stack now
        has the agent and the archive command, or no longer has them, and
        only the database settings are left. Doing it here spares the
        second rebuild the watch would queue.

        A rebuild whose update of the modules ended well (the context's
        ``ic_modules_updated``, set by the rebuild) also settles a restore
        to a moment whose database could not be updated to the code
        (``ALERT_MODULES``). A flow that renders the stack without that
        update (a flow that skips the step, a move's deploy) does not.

        It also keeps where the stack was told to archive
        (``_pitr_record_rendered``).
        """
        super()._after_stack_rendered()
        self._pitr_record_rendered()
        self._pitr_reconcile(after_rebuild=True)
        if self.env.context.get("ic_modules_updated"):
            self.env["cloud.alert"].resolve_alert(ALERT_MODULES, instance=self)

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
        if self._pitr_active():
            if self._pitr_purge_due():
                # Wanted on again once its archive's date came: the watch
                # empties it first (``_pitr_queue_due_purges``), and the
                # pass after switches it on from nothing.
                return None
            # Wanted on again before then (a plan raised, the field set):
            # the archive is kept, and a purge still waiting would hold
            # the switch.
            self._pitr_cancel_purge()
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

    # ── Where it archives, and the archive once switched off ────────────

    def _pitr_record_rendered(self):
        """Keep the prefix and destination the stack was just given.

        Read once a rebuild wrote ``pitr.env`` (``_pitr_writes_env``): what
        the server archives to, and what the purge of an instance switched
        off empties (``_pitr_purge_target``). A prefix that differs from
        the one last recorded (a backend changed, the project's folder
        renamed, a bucket edited), or the same prefix on another
        destination, leaves what was archived before where it was, with
        nothing that empties it: ``pitr_prefix_changed`` says where
        (``_pitr_alert_prefix_changed``).

        Switched off with a prefix kept, it is left as it is: nothing
        archives at the new one, and the purge of the archive switched
        off empties the one it was archived at. So while it waits for that
        purge, wanted on again once its date came. Switched on again, the
        rebuild that renders the new one raises the alert; after the
        purge, which forgets the prefix, it is only kept.
        """
        self.ensure_one()
        if not self._pitr_writes_env():
            return
        if self.pitr_rendered_prefix and (
            not self._pitr_renders() or self._pitr_purge_due()
        ):
            return
        backend = self.effective_backup_backend
        prefix = self._pitr_prefix_base()
        old_prefix = self.pitr_rendered_prefix
        old_backend = self.pitr_rendered_backend_id
        if old_prefix == prefix and old_backend == backend:
            return
        if old_prefix and (
            old_prefix != prefix or (old_backend and old_backend != backend)
        ):
            self._pitr_alert_prefix_changed(old_prefix, old_backend, prefix)
        self.sudo().write({
            "pitr_rendered_prefix": prefix,
            "pitr_rendered_backend_id": backend.id,
        })

    def _pitr_alert_prefix_changed(self, old_prefix, old_backend, prefix):
        """Raise ``pitr_prefix_changed`` for the prefix a rebuild left.

        On the instance, keyed by the old prefix: each change leaves one.
        Restores to a moment before the change may still need it for a
        backup window; from then on a person empties it (RB-23 §4). To the
        service provider when that destination is managed.

        :param str old_prefix: the prefix rendered before
        :param old_backend: the ``cloud.backup.backend`` it was on, possibly
            empty (deleted since)
        :param str prefix: the prefix rendered now
        """
        self.ensure_one()
        until = fields.Datetime.now() + timedelta(days=self._pitr_window_days())
        where = old_backend.name if old_backend else "a destination since deleted"
        managed = bool(old_backend and old_backend.sudo().managed_reservation_id)
        self.env["cloud.alert"].sudo().raise_alert(
            ALERT_PREFIX_CHANGED,
            f"The continuous recovery archive of '{self.name}' now goes to "
            f"{prefix}; what it archived before stays at {old_prefix} on "
            f"'{where}', and nothing empties it. A restore to a moment "
            f"before the change may need it until "
            f"{fields.Datetime.to_string(until)} UTC; from then on, empty "
            "that prefix by hand (RB-23 §4), then resolve this alert.",
            level="warning", instance=self, key=old_prefix,
            audience="provider" if managed else "fleet",
        )

    def _pitr_purge_target(self):
        """Where the archive of an instance switched off is.

        The prefix and destination last rendered (``_pitr_record_rendered``):
        what its server archived to. One rendered before they were kept
        falls back to the computed prefix on the destination it has now.

        :return: ``(backend, prefix)``; *prefix* is ``""`` with no S3
            destination to name it
        :rtype: tuple
        """
        self.ensure_one()
        if self.pitr_rendered_prefix:
            return (
                self.pitr_rendered_backend_id or self.effective_backup_backend,
                self.pitr_rendered_prefix,
            )
        backend = self.effective_backup_backend
        if not (backend and backend.backend_type == "s3" and backend.s3_bucket):
            return backend, ""
        return backend, self._pitr_prefix_base()

    def _pitr_schedule_purge(self, again=False):
        """Fix the day the archive of an instance switched off is emptied.

        Now plus the backup window it has now: fixed, never recalculated,
        as a plan that lowers the retention also switches it off. Only for
        an instance with an archive (a cluster recorded, or a prefix
        rendered); a date already fixed is kept unless *again*.

        :param bool again: date it from now even with a date fixed: what
            ``pitr_disable`` does, as archiving went on until it ran (a
            switch on that did not clear the dates, such as a move's)
        """
        self.ensure_one()
        if (self.pitr_purge_at and not again) or not (
            self.pitr_cluster_id or self.pitr_rendered_prefix
        ):
            return
        off_at = fields.Datetime.now()
        self.sudo().write({
            "pitr_off_at": off_at,
            "pitr_purge_at": off_at + timedelta(days=self._pitr_window_days()),
        })

    def _pitr_cancel_purge(self):
        """Keep the archive: the instance is to archive again.

        Before the purge date only: clears it and cancels a ``pitr_purge``
        still waiting in the queue. Once the date came, nothing: the purge
        may have started, and the program deletes the oldest base copies
        first, so a purge cut short leaves deltas without their base, on
        which a switch on would take its first copy. That archive is
        emptied first, then switched on from nothing (``_pitr_reconcile``).
        """
        self.ensure_one()
        if not (self.pitr_off_at or self.pitr_purge_at) or self._pitr_purge_due():
            return
        self.sudo().write({"pitr_off_at": False, "pitr_purge_at": False})
        waiting = self._pitr_jobs_in_flight(("pitr_purge",)).filtered(
            lambda job: job.state == "queued",
        )
        if waiting:
            self.env["cloud.job"].sudo()._cancel_queued(waiting, _(
                "Cancelled: continuous recovery is switched on again, and "
                "its archive is kept.",
            ))
        self.env["cloud.alert"].sudo().resolve_alert(
            ALERT_PURGE_FAILED, instance=self,
        )

    def _pitr_purge_due(self):
        """Whether the purge date of the archive switched off has come.

        :rtype: bool
        """
        self.ensure_one()
        return bool(
            self.pitr_purge_at and self.pitr_purge_at <= fields.Datetime.now()
        )

    def _pitr_purge_pending(self, before=None):
        """Whether the archive is to be emptied as switched off, now.

        Not archiving, with a purge date that came: also when wanted on
        again since, as a switch on waits for the purge then
        (``_pitr_cancel_purge``). With *before*, switched off at that
        moment, the cutoff a purge was queued with. The job checks it
        too: one queued by hand before the date empties nothing.

        :param str before: ``pitr_off_at`` as a string, or None
        :rtype: bool
        """
        self.ensure_one()
        if not (self.pitr_off_at and self._pitr_purge_due()):
            return False
        if (self.pitr_state or "off") != "off":
            return False
        return before is None or fields.Datetime.to_string(self.pitr_off_at) == before

    def _pitr_forget_archive(self):
        """The archive of an instance switched off is empty: forget it.

        Its clusters, base copies and restores, the purge date and the
        prefix it was rendered at. With no cluster left, no rebuild
        writes ``pitr.env`` again (``_pitr_writes_env``).
        """
        self.ensure_one()
        self.sudo().write({
            "pitr_cluster_id": False, "pitr_previous_clusters": False,
            "pitr_timelines": False, "pitr_oldest_base_at": False,
            "pitr_last_base_at": False, "pitr_off_at": False,
            "pitr_purge_at": False, "pitr_rendered_prefix": False,
            "pitr_rendered_backend_id": False,
        })

    @api.model
    def _pitr_schedule_leftover_purges(self):
        """Fix the purge date of instances switched off without one.

        Switched off before the purge existed, or without a ``pitr_disable``
        (archiving had already stopped): not archiving, not wanted to,
        with a cluster recorded. Not a stack configured outside the panel
        (its runbook), nor an archived instance (its archive goes with the
        record, ``_archived_copy_extra_prefixes``). One instance that fails
        does not stop the others. Dated from now, the update's too: the
        window it gets is the one a switch off gives, whenever it was.

        :return: how many got a date
        :rtype: int
        """
        found = self.sudo().search([
            ("pitr_cluster_id", "!=", False), ("pitr_purge_at", "=", False),
            ("pitr_stack_external", "=", False),
            ("pitr_state", "in", (False, "off")),
        ])
        scheduled = 0
        for inst in found:
            try:
                with self.env.cr.savepoint():
                    if inst._pitr_active():
                        continue
                    inst._pitr_schedule_purge()
                    scheduled += 1
            except Exception as exc:  # noqa: BLE001 — one must not stop all
                _logger.warning(
                    "Continuous recovery purge date skipped %s: %s", inst.name, exc,
                )
        return scheduled

    @api.model
    def _pitr_queue_due_purges(self):
        """Queue the purge of every archive whose date has come.

        Instances switched off (``_pitr_purge_pending``), deployed on a
        server, not moving, not a stack configured outside the panel; not
        beside another job of this module, nor within a day of a purge
        that failed. The cutoff goes with the job, which empties nothing
        if it is no longer the instance's when it starts. One instance that
        fails does not stop the others.

        :return: how many got a purge
        :rtype: int
        """
        Job = self.env["cloud.job"].sudo()
        queued = 0
        for inst in self.sudo().search([
            ("pitr_purge_at", "<=", fields.Datetime.now()),
            ("deployed", "=", True), ("host_id", "!=", False),
            ("move_origin_host_id", "=", False),
            ("pitr_stack_external", "=", False),
        ] + self._periodic_maintenance_domain()):
            try:
                with self.env.cr.savepoint():
                    if not inst._pitr_purge_pending() \
                            or inst._pitr_job_in_flight(PITR_JOBS) \
                            or inst._pitr_failed_lately("pitr_purge"):
                        continue
                    Job.enqueue(inst.host_id.id, inst.id, "pitr_purge", payload={
                        "before": fields.Datetime.to_string(inst.pitr_off_at),
                    })
                    queued += 1
            except Exception as exc:  # noqa: BLE001 — one must not stop all
                _logger.warning(
                    "Continuous recovery purge skipped %s: %s", inst.name, exc,
                )
        return queued

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

        A layer that rebuilds its instances with its own executor answers
        through ``cloud.github.event._rebuild_job_type``.

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
        (one restart of the database) and takes the first base copy. An
        archive switched off is kept before its purge date
        (``_pitr_cancel_purge``); once it came, the switch waits for the
        purge, which may have started.

        :param str objective: optional new objective (minutes)
        :return: the first job's id
        :rtype: int
        :raise UserError: while the archive switched off is past its date
        """
        self.ensure_one()
        self._pitr_check_manager()
        self._pitr_refuse_external()
        if not self._pitr_postgres_supported():
            raise UserError(_("Continuous recovery needs PostgreSQL 12 or later."))
        if not self._pitr_applicable():
            raise UserError(_(
                "Continuous recovery needs a production instance with an "
                "S3 backup backend.",
            ))
        self._pitr_refuse_busy()
        if self._pitr_purge_due():
            raise UserError(_(
                "The archive kept when continuous recovery was switched off "
                "is past its date and is being emptied. Switch it on again "
                "once that is done: it starts a new archive.",
            ))
        if objective and str(objective) not in dict(OBJECTIVES):
            raise UserError(_("Unknown recovery objective: %s", objective))
        self._pitr_refuse_name_taken()
        self._pitr_ensure_key()
        self._pitr_cancel_purge()
        vals = {"pitr_enabled": True, "pitr_state": "enabling"}
        if objective:
            vals["pitr_objective"] = str(objective)
        self.sudo().write(vals)
        return self._pitr_enqueue([self._pitr_rebuild_job_type(), "pitr_enable"])

    def _pitr_refuse_name_taken(self):
        """Refuse to archive where another instance keeps its backups.

        The archive goes under the instance's name and ``-pitr``, in its
        backup folder (``_pitr_bucket_path``): an instance named so, in
        the same folder, active or archived, keeps its duplicity chain
        there, and emptying either would empty both. Core refuses such a
        name from now on; this covers one created before.

        :raise UserError: when there is one
        """
        self.ensure_one()
        folder = self.project_id.remote_folder or "default"
        taken = self.with_context(active_test=False).sudo().search([
            ("name", "=", f"{self.name}{self.backup_path_suffix or ''}-pitr"),
            ("id", "!=", self.id),
        ]).filtered(
            lambda other: (other.project_id.remote_folder or "default") == folder
        )
        if taken:
            raise UserError(_(
                "Continuous recovery cannot be switched on for %(name)s: its "
                "archive would be kept where the instance %(other)s, in the "
                "same backup folder, keeps its backups. Rename one of the two first.",
                name=self.name, other=taken[0].name,
            ))

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

        What is in the bucket stays for the backup window the instance
        has when archiving stops, then ``pitr_purge`` empties it
        (``_pitr_schedule_purge``, at the end of ``pitr_disable``).

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
            "postgres_supported": self._pitr_postgres_supported(),
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
        does not stop the others: each runs in its own savepoint. Then
        the archives of instances switched off: a purge date for those
        without one (``_pitr_schedule_leftover_purges``), the purge for
        those whose date has come (``_pitr_queue_due_purges``).

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
        self._pitr_schedule_leftover_purges()
        return queued + self._pitr_queue_due_purges()
