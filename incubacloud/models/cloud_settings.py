from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .encrypted_char import EncryptedChar


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
        help=(
            "PAT used as fallback when no GitHub App is configured. "
            "Stored encrypted — never exposed in logs or API responses."
        ),
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
