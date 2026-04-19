import logging
import os

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

from .encrypted_char import EncryptedChar
from .password_utils import key_is_configured

_logger = logging.getLogger(__name__)


class CloudSettings(models.Model):
    """System-wide IncubaCloud settings — singleton.

    Stores sensitive credentials that must not live in
    ``ir.config_parameter`` (plain text in the database).
    Fields use ``EncryptedChar`` (Fernet) so the DB column
    always holds ``enc:<token>``.
    """

    _name = "cloud.settings"
    _inherit = ["cloud.security.mixin"]
    _description = "IncubaCloud Settings"

    # ── GitHub credentials ─────────────────────────────────────────────────

    github_pat = EncryptedChar(
        string="GitHub Personal Access Token",
        groups="incubacloud.group_cloud_manager",
        help=(
            "PAT used as fallback when no GitHub App is configured. "
            "Stored encrypted — never exposed in logs or API responses."
        ),
    )

    # ── Job log retention ──────────────────────────────────────────────────
    # cloud.job.log.chunk grows monotonically (every SSH command stream
    # writes hundreds of rows per job). The daily purge cron deletes
    # chunks older than this many days that belong to terminal jobs.
    # Active jobs always keep their full log regardless of age.

    job_log_retention_days = fields.Integer(
        string='Job log retention (days)',
        default=30,
        help='Number of days to keep job log chunks (cloud.job.log.chunk) '
             'for terminated jobs. Active jobs are never purged. Set to 0 '
             'to disable purging entirely (table will grow unbounded).',
    )

    # ── Singleton constraint ───────────────────────────────────────────────

    @api.constrains("github_pat")
    def _check_singleton(self):
        if self.env["cloud.settings"].search_count([]) > 1:
            raise UserError(
                _("Only one IncubaCloud settings record is allowed.")
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    @api.model
    def _get(self):
        """Return the singleton record, creating it if needed."""
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({})
        return rec

    # ── Startup check: INCUBACLOUD_SECRET_KEY ──────────────────────────────

    def _register_hook(self):
        """Warn loudly at registry load if the encryption key is missing.

        The actual fail-loud behaviour lives in ``password_utils``: any
        attempt to encrypt/decrypt raises. This hook only surfaces the
        misconfiguration at startup so operators don't discover it at the
        first write. Suppressed during tests (the key is patched per-test)
        and behind ``INCUBACLOUD_ALLOW_NO_KEY=1`` for ad-hoc dev scripts
        that deliberately avoid the key.
        """
        super()._register_hook()
        if tools.config.get('test_enable'):
            return
        if os.environ.get('INCUBACLOUD_ALLOW_NO_KEY'):
            _logger.warning(
                "INCUBACLOUD_ALLOW_NO_KEY=1 — any write to an encrypted "
                "field will still raise at runtime."
            )
            return
        if not key_is_configured():
            _logger.critical(
                "INCUBACLOUD_SECRET_KEY is missing or invalid. Any write to "
                "cloud.host.password, cloud.instance.*_password, "
                "cloud.backup.backend.passphrase, cloud.settings.github_pat "
                "and similar fields will raise. Configure the variable in "
                "the Odoo container environment before going to production."
            )
