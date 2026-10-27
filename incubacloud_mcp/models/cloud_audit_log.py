"""The audit says when an action came through an agent, and which."""

from odoo import api, fields, models

from ..protocol.context import VIA_CONTEXT_KEY


class CloudAuditLog(models.Model):
    """Audit rows with the agent that caused them, if any."""

    _inherit = "cloud.audit.log"

    via_client = fields.Char(
        readonly=True,
        help="Set when the action came through the MCP endpoint: the "
        "verified client id of the token, or 'API key <name>', then "
        "between brackets the name the client declares (not verified).",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Stamp the rows of an agent's request; drop a forged stamp."""
        via = self.env.context.get(VIA_CONTEXT_KEY)
        for vals in vals_list:
            if via:
                vals["via_client"] = via
            elif not self.env.su:
                vals.pop("via_client", None)
        return super().create(vals_list)

    def _format(self):
        """Add ``via_client`` to each entry the panel lists."""
        rows = super()._format()
        via_by_id = {entry.id: entry.via_client or "" for entry in self}
        for row in rows:
            row["via_client"] = via_by_id.get(row["id"], "")
        return rows
