import logging
from datetime import timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from odoo import _, api, fields, models

from .encrypted_char import EncryptedChar
from .password_utils import generate_password

_logger = logging.getLogger(__name__)

# Re-alert cadence while a backend stays above its threshold. Daily
# would feel like spam; weekly is enough for the operator to react
# without being noisy.
_ALERT_DEBOUNCE_DAYS = 7


class CloudBackupBackend(models.Model):
    _name = "cloud.backup.backend"
    _inherit = ["cloud.security.mixin"]
    _description = "Backup Backend"

    name = fields.Char(required=True)
    backend_type = fields.Selection(
        selection=[("s3", "S3 Compatible")],
        string="Type",
        required=True,
        default="s3",
    )

    # ── S3 / compatible storage ─────────────────────────────────────────────

    s3_bucket = fields.Char(string="Bucket")
    s3_path = fields.Char(
        string="Path",
        default="backups",
        help="Object prefix inside the bucket (e.g. backups or clients/acme).",
    )
    s3_endpoint_url = fields.Char(
        string="Endpoint URL",
        help="Leave empty for AWS S3. Set for MinIO, Wasabi, Backblaze, etc.",
    )
    s3_access_key_id = fields.Char(
        string="Access Key ID",
        groups="incubacloud.group_cloud_manager",
    )
    s3_secret_access_key = EncryptedChar(
        string="Secret Access Key",
        groups="incubacloud.group_cloud_manager",
    )

    # ── Encryption ─────────────────────────────────────────────────────────

    passphrase = EncryptedChar(
        string="Encryption Passphrase",
        groups="incubacloud.group_cloud_manager",
        help="Auto-generated if left blank. Used as PASSPHRASE inside the backup container.",
    )

    # ── Docker image ────────────────────────────────────────────────────────

    backup_image_version = fields.Char(
        string="Image Version",
        default="latest",
        help="Tag of the docker-duplicity image (e.g. latest, 4.0.0).",
    )

    # ── Notifications ───────────────────────────────────────────────────────

    email_from = fields.Char(string="Notification From")
    email_to = fields.Char(string="Notification To")
    smtp_report_success = fields.Boolean(
        string="Report on Success",
        default=True,
    )

    # ── Retention ─────────────────────────────────────────────────────────────

    backup_retention = fields.Char(
        string="Retention Period",
        default="3M",
        help="How long to keep old backups (e.g. 3M = 3 months, 30D = 30 days, 1Y = 1 year).",
    )

    # ── Advanced ────────────────────────────────────────────────────────────

    retention_owner = fields.Selection(
        [
            ("cron", "Duplicity cron"),
            ("lifecycle", "Bucket lifecycle"),
            ("none", "Nobody — infinite archive"),
        ],
        default="cron",
        required=True,
        help="Who deletes old backups: the daily deletion cron inside "
        "the container, a bucket lifecycle policy configured "
        "outside IncubaCloud, or nobody (backups are kept forever).",
    )
    retention_owner_declared = fields.Boolean(
        default=True,
        help="False marks an ambiguous, un-confirmed retention state "
        "(migrated from the old on/off flag with the flag off, "
        "with no lifecycle explicitly declared). Triggers a "
        "warning alert until the operator picks an explicit "
        "retention_owner value.",
    )
    backup_tz = fields.Char(
        string="Timezone",
        default="UTC",
    )

    # ── Computed destination URL ────────────────────────────────────────────

    backup_dst = fields.Char(
        string="Backup Path",
        compute="_compute_backup_dst",
        store=True,
        help="Computed duplicity DST URL (e.g. boto3+s3://bucket/path).",
    )

    @api.depends("backend_type", "s3_bucket", "s3_path")
    def _compute_backup_dst(self):
        for rec in self:
            if rec.backend_type == "s3" and rec.s3_bucket:
                path = (rec.s3_path or "").strip("/")
                if path:
                    rec.backup_dst = f"boto3+s3://{rec.s3_bucket}/{path}"
                else:
                    rec.backup_dst = f"boto3+s3://{rec.s3_bucket}"
            else:
                rec.backup_dst = ""

    # ── Usage tracking + quota alerts ──────────────────────────────────────
    # Universal feature: applies to every backend, BYO or managed. The
    # tenant-side Odoo also gets it (the ``incubacloud`` core ships inside
    # tenant instances), so any backend the tenant has configured benefits.

    quota_gb = fields.Float(
        string="Quota (GB)",
        default=0.0,
        help="Soft cap used for alert calculations. Set to 0 to disable "
        "quota tracking (usage is still measured for display).",
    )
    last_measured_gb = fields.Float(
        string="Last Measured (GB)",
        readonly=True,
    )
    last_measured_at = fields.Datetime(
        string="Last Measured At",
        readonly=True,
    )
    alert_threshold_pct = fields.Integer(
        string="Alert Threshold (%)",
        default=0,
        help="Per-backend override of the system-wide default alert "
        "threshold. 0 = inherit from settings; -1 = disabled; "
        "1–100 = explicit percentage.",
    )
    alert_email = fields.Char(
        string="Alert Email",
        help="Optional override of the recipient for usage alerts. "
        "Falls back to ``Notification To`` if empty.",
    )
    last_alert_sent_at = fields.Datetime(
        string="Last Alert Sent At",
        readonly=True,
    )

    # ── Externally-managed backend association (opaque hook field) ────────
    # When non-empty, this backend is managed by an external module that
    # sets this opaque id and overrides ``_on_managed_backend_delete``.
    # Core stores the id but never interprets it and knows nothing about
    # the managing module — empty for self-managed (BYO) backends.

    managed_reservation_id = fields.Char(
        string="Managed Reservation ID",
        readonly=True,
        copy=False,
        help="Opaque id set by an external module that manages this "
        "backend. Core does not interpret it. Empty for BYO backends.",
    )
    archived_until = fields.Datetime(
        string="Archived Until",
        readonly=True,
        copy=False,
        help="When set, this backend is in retention period — the bucket "
        "still works for backup_download but new backups should not "
        "be written here. The tenant cron unlinks the record when "
        "the manager confirms the bucket has been purged.",
    )

    # ── Password handling ───────────────────────────────────────────────────

    _PASSWORD_FIELDS = ("s3_secret_access_key", "passphrase")

    def write(self, vals):
        self._check_can_manage_settings()
        for field in self._PASSWORD_FIELDS:
            if field in vals and not vals[field]:
                del vals[field]
        res = super().write(vals)
        # An explicit user choice of retention_owner resolves the
        # ambiguous state and its warning alert. Migrations bypass the
        # ORM (raw SQL), so this only fires for real operator edits.
        if "retention_owner" in vals and "retention_owner_declared" not in vals:
            self.write({"retention_owner_declared": True})
            self._resolve_retention_undeclared_alert()
        return res

    def unlink(self):
        self._check_can_manage_settings()
        return super().unlink()

    def _on_managed_backend_delete(self):
        """Hook: convert a delete of an externally-managed backend into the
        managing module's own teardown.

        Called by the delete endpoint when ``managed_reservation_id`` is
        set. The managing module (which set that id) overrides this to run
        its teardown and returns a JSON-RPC result dict. Core has no
        managed-backup concept, so by default such a backend cannot be
        deleted here.
        """
        self.ensure_one()
        return {
            "ok": False,
            "error": _("This managed destination cannot be deleted from this database."),
        }

    @api.model_create_multi
    def create(self, vals_list):
        self._check_can_manage_settings()
        for vals in vals_list:
            if not vals.get("passphrase"):
                vals["passphrase"] = generate_password()
        return super().create(vals_list)

    # ── Effective alert threshold ──────────────────────────────────────────

    def _effective_alert_threshold(self):
        """Resolve the threshold % that applies to *self*.

        Returns ``None`` when alerts are disabled (own field is -1, or
        own field is 0 and the settings default is also 0/-1, or no
        quota is set on the backend). Otherwise returns the percentage
        in the 1–100 range to compare ``last_measured_gb / quota_gb``
        against.
        """
        self.ensure_one()
        if not self.quota_gb or self.quota_gb <= 0:
            return None
        own = self.alert_threshold_pct
        if own == -1:
            return None
        if own > 0:
            return min(own, 100)
        # Inherit
        default = (
            self.env["cloud.settings"]
            .sudo()
            ._get_system()
            .default_backup_alert_threshold_pct
        )
        if default <= 0:
            return None
        return min(default, 100)

    # ── Usage measurement (boto3 ListObjectsV2 + sum) ──────────────────────

    def _measure_usage(self):
        """Refresh ``last_measured_gb`` / ``last_measured_at`` for *self*.

        Paginated ``ListObjectsV2`` summing ``Size``. S3-compatible ops
        are free on Cloudflare R2 and most providers; AWS S3 charges
        per-1k LIST but a daily cron at small object counts stays in
        the cents.

        Raises ``BotoCoreError`` / ``ClientError`` upwards — the caller
        (cron or on-demand endpoint) decides whether to swallow.
        """
        self.ensure_one()
        if self.backend_type != "s3" or not self.s3_bucket:
            return
        if not self.s3_access_key_id or not self.s3_secret_access_key:
            return
        kwargs = {
            "aws_access_key_id": self.s3_access_key_id,
            "aws_secret_access_key": self.s3_secret_access_key,
        }
        if self.s3_endpoint_url:
            kwargs["endpoint_url"] = self.s3_endpoint_url
        client = boto3.client("s3", **kwargs)
        prefix = (self.s3_path or "").strip("/")
        total = 0
        paginator = client.get_paginator("list_objects_v2")
        page_kwargs = {"Bucket": self.s3_bucket}
        if prefix:
            page_kwargs["Prefix"] = f"{prefix}/"
        for page in paginator.paginate(**page_kwargs):
            for obj in page.get("Contents") or ():
                total += obj.get("Size") or 0
        # 1 GB = 1024**3 bytes. Backups are typically reported in IEC.
        gb = total / (1024**3)
        self.sudo().write(
            {
                "last_measured_gb": gb,
                "last_measured_at": fields.Datetime.now(),
            }
        )

    def _maybe_send_threshold_alert(self):
        """Send the usage-alert email if *self* is above its threshold
        and the 7-day debounce has elapsed.

        Idempotent: callable from any cron pass without re-firing.
        """
        self.ensure_one()
        threshold_pct = self._effective_alert_threshold()
        if threshold_pct is None:
            return
        if not self.last_measured_gb or self.last_measured_gb <= 0:
            return
        pct_used = (self.last_measured_gb / self.quota_gb) * 100
        if pct_used < threshold_pct:
            return
        # Debounce.
        if self.last_alert_sent_at:
            elapsed = fields.Datetime.now() - self.last_alert_sent_at
            if elapsed < timedelta(days=_ALERT_DEBOUNCE_DAYS):
                return
        recipient = (self.alert_email or self.email_to or "").strip()
        if not recipient:
            _logger.warning(
                "Backup backend %s reached %.1f%% usage but has no "
                "alert recipient (alert_email/email_to both empty). "
                "Set one to receive alerts.",
                self.display_name,
                pct_used,
            )
            return
        template = self.env.ref(
            "incubacloud.mail_template_backup_usage_alert",
            raise_if_not_found=False,
        )
        if not template:
            _logger.error(
                "Cannot send backup usage alert for %s: mail template "
                "'incubacloud.mail_template_backup_usage_alert' is missing.",
                self.display_name,
            )
            return
        template.sudo().with_context(
            backup_alert_pct_used=round(pct_used, 1),
            backup_alert_threshold_pct=threshold_pct,
        ).send_mail(
            self.id,
            force_send=False,
            email_values={
                "email_to": recipient,
            },
        )
        self.sudo().write({"last_alert_sent_at": fields.Datetime.now()})

    @api.model
    def _cron_measure_backup_usage(self):
        """Daily cron: measure usage for every S3 backend and alert on threshold.

        Per-backend try/except — one failing backend (bad creds, network,
        missing bucket) must not abort the loop.
        """
        backends = self.sudo().search(
            [
                ("backend_type", "=", "s3"),
                ("s3_bucket", "!=", False),
                ("s3_access_key_id", "!=", False),
            ]
        )
        ok, failed = 0, 0
        for backend in backends:
            try:
                backend._measure_usage()
                backend._maybe_send_threshold_alert()
                ok += 1
            except (BotoCoreError, ClientError) as exc:
                failed += 1
                _logger.warning(
                    "[backup-usage-cron] %s: measurement failed: %s",
                    backend.display_name,
                    exc,
                )
            except Exception:  # noqa: BLE001
                failed += 1
                _logger.exception(
                    "[backup-usage-cron] %s: unexpected failure",
                    backend.display_name,
                )
        _logger.info(
            "[backup-usage-cron] measured %s backend(s), %s failed",
            ok,
            failed,
        )
        return {"ok": ok, "failed": failed}

    # ── Retention-owner ambiguous-state alert ───────────────────────────────
    # cloud.alert has no backup_backend_id column — backend-scoped alerts
    # (no host/instance/project target) are matched via the generic
    # ``payload`` field instead of adding a new FK for two alert codes.

    _RETENTION_ALERT_CODE = "backup_retention_undeclared"

    def _find_backend_alert(self, code):
        """Return the active alert for *self* with *code*, matched via
        ``payload['backup_backend_id']`` (self is a single record)."""
        self.ensure_one()
        return (
            self.env["cloud.alert"]
            .sudo()
            .search(
                [
                    ("code", "=", code),
                    ("state", "=", "active"),
                ]
            )
            .filtered(
                lambda a: (a.payload or {}).get("backup_backend_id") == self.id,
            )
        )

    def _resolve_retention_undeclared_alert(self):
        self.ensure_one()
        self._find_backend_alert(self._RETENTION_ALERT_CODE).write(
            {
                "state": "dismissed",
            }
        )

    @api.model
    def _cron_check_retention_declared(self):
        """Daily cron: warn on backends whose retention owner is still
        ambiguous (migrated from the old on/off flag with the flag off,
        or otherwise never explicitly confirmed by the operator).

        Declaring an owner via ``write()`` resolves the alert; this
        cron never auto-resolves anything, only creates/refreshes.
        """
        Alert = self.env["cloud.alert"].sudo()
        undeclared = self.search([("retention_owner_declared", "=", False)])
        for backend in undeclared:
            existing = backend._find_backend_alert(self._RETENTION_ALERT_CODE)
            vals = {
                "code": self._RETENTION_ALERT_CODE,
                "level": "warning",
                "message": (
                    f"Backup backend '{backend.display_name}' has an "
                    f"unconfirmed retention policy — old backups may "
                    f"never be deleted. Open it and pick who owns "
                    f"retention (Duplicity cron / bucket lifecycle / "
                    f"nobody)."
                ),
                "payload": {"backup_backend_id": backend.id},
            }
            if existing:
                existing.write(vals)
            else:
                Alert.create(vals)
        return {"flagged": len(undeclared)}

    # ── Managed backend orphan alert (P15 #9b) ──────────────────────────────
    # Core only knows ``managed_reservation_id`` is non-empty ("this backend
    # is managed by some external module"); it never interprets the id
    # itself, so this check stays valid regardless of who manages it.

    _MANAGED_UNUSED_ALERT_CODE = "managed_backend_unused"

    @api.model
    def _cron_check_managed_unused(self):
        """Daily cron: warn on managed backends assigned to nothing —
        not an instance, not a project default, not the global default.

        Never auto-resolves for backends that vanish from the search
        (assigning one dismisses its own alert instead, see below).
        """
        Alert = self.env["cloud.alert"].sudo()
        managed = self.search([("managed_reservation_id", "!=", False)])
        if not managed:
            return {"flagged": 0}

        used_ids = set(
            self.env["cloud.instance"]
            .sudo()
            .search(
                [
                    ("backup_backend_id", "in", managed.ids),
                ]
            )
            .mapped("backup_backend_id")
            .ids
        )
        used_ids |= set(
            self.env["cloud.project"]
            .sudo()
            .search(
                [
                    ("backup_backend_id", "in", managed.ids),
                ]
            )
            .mapped("backup_backend_id")
            .ids
        )
        global_id = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "incubacloud.backup_backend_id",
                0,
            )
            or 0
        )
        if global_id:
            used_ids.add(global_id)

        flagged = 0
        for backend in managed:
            existing = backend._find_backend_alert(
                self._MANAGED_UNUSED_ALERT_CODE,
            )
            if backend.id in used_ids:
                existing.write({"state": "dismissed"})
                continue
            flagged += 1
            vals = {
                "code": self._MANAGED_UNUSED_ALERT_CODE,
                "level": "warning",
                "message": (
                    f"Managed backup backend '{backend.display_name}' "
                    f"is not assigned to any instance, project or the "
                    f"global default — it's being paid for and unused."
                ),
                "payload": {"backup_backend_id": backend.id},
            }
            if existing:
                existing.write(vals)
            else:
                Alert.create(vals)
        return {"flagged": flagged}
