"""Archiving a user ends their auto-approval rules."""

from odoo import models


class ResUsers(models.Model):
    """Users whose auto-approval rules end when they are archived."""

    _inherit = "res.users"

    def write(self, vals):
        """Revoke the rules of the users archived, so they do not come back
        if the user is reactivated."""
        result = super().write(vals)
        if "active" in vals and not vals["active"]:
            self.env["incubacloud.mcp.auto.approval"].sudo().search(
                [("user_id", "in", self.ids), ("revoked_at", "=", False)]
            )._revoke("user archived")
        return result
