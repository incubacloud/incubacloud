import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..github.credentials import GitHubAppCredentials
from .encrypted_char import EncryptedChar, EncryptedText

_logger = logging.getLogger(__name__)


class CloudGitHubApp(models.Model):
    """GitHub App configuration — system-level singleton.

    In BYO mode there is exactly one record in this model, shared across the
    whole Odoo instance.  The private key is stored encrypted at rest and is
    never returned to front-end clients via JSON-RPC; instead, callers receive
    only a ``has_private_key`` boolean flag.
    """

    _name = "cloud.github.app"
    _inherit = ["cloud.security.mixin"]
    _description = "GitHub App Configuration"
    _rec_name = "app_id"

    app_id = fields.Char(
        string="App ID",
        required=True,
        help="Numeric identifier of the GitHub App (found in App settings).",
    )
    installation_id = fields.Char(
        string="Installation ID",
        help=(
            "ID of the GitHub App installation on your organisation or account."
            " Found under Settings → GitHub Apps → Configure → URL parameter."
            " Auto-detected from the first installation webhook if left empty."
        ),
    )
    private_key = EncryptedText(
        string="Private Key (PEM)",
        required=True,
        groups="base.group_system",
        help=(
            "RSA private key in PEM format generated for this GitHub App."
            " Stored encrypted at rest; never exposed in logs or API"
            " responses."
        ),
    )
    webhook_secret = EncryptedChar(
        string="Webhook Secret",
        groups="incubacloud.group_cloud_manager",
        help=(
            "Secret used to validate incoming webhook HMAC-SHA256 signatures."
            " Must match the value configured in the GitHub App webhook settings."
        ),
    )
    slug = fields.Char(
        string="App Slug",
        readonly=True,
        help=(
            "GitHub App slug (URL-safe name). Auto-set during manifest flow. "
            "Used to generate the direct org installation link."
        ),
    )

    # ── Singleton constraint ────────────────────────────────────────────────

    @api.constrains("app_id")
    def _check_singleton(self):
        if self.env["cloud.github.app"].search_count([]) > 1:
            raise UserError(
                _("Only one GitHub App configuration is allowed per system.")
            )

    # ── Security checks ──────────────────────────────────────────────────────

    def write(self, vals):
        self._check_can_manage_settings()
        return super().write(vals)

    def unlink(self):
        self._check_can_manage_settings()
        return super().unlink()

    # ── Credential helpers ──────────────────────────────────────────────────

    def _get_credentials(self):
        """Return a GitHubAppCredentials instance from this record."""
        self.ensure_one()
        return GitHubAppCredentials(
            app_id=self.app_id,
            installation_id=self.installation_id,
            private_key_pem=self.sudo().private_key,
            webhook_secret=self.webhook_secret or "",
        )

    # ── Installation auto-detection ─────────────────────────────────────────

    @api.model
    def _process_installation_event(self, payload: dict) -> bool:
        """Auto-set installation_id from an installation.created webhook.

        Called by the webhook controller when it receives an
        ``installation`` event with action ``created``.

        Returns ``True`` if the installation_id was updated.
        """
        installation_id = str(
            payload.get("installation", {}).get("id", "")
        )
        if not installation_id:
            return False

        app = self.search([], limit=1)
        if not app:
            return False

        if not app.installation_id:
            app.write({"installation_id": installation_id})
            _logger.info(
                "GitHub App installation_id auto-set to %s", installation_id
            )
            return True

        return False

    # ── Odoo backend action ─────────────────────────────────────────────────

    def action_test_connection(self):
        """Test the GitHub App connection from the Odoo backend form view."""
        self.ensure_one()
        service = self.env["cloud.github.credential.service"]
        result = service.test_connection()
        if result.get("ok"):
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connection Successful"),
                    "message": _(
                        "Connected to GitHub App '%(name)s' (ID: %(id)s).",
                        name=result.get("app_name", ""),
                        id=result.get("app_id", ""),
                    ),
                    "type": "success",
                    "sticky": False,
                },
            }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection Failed"),
                "message": result.get("error", _("Unknown error.")),
                "type": "danger",
                "sticky": True,
            },
        }
