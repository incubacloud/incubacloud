"""A job queued by an agent says so."""

from odoo import api, fields, models
from odoo.exceptions import AccessError

from ..protocol.context import VIA_CONTEXT_KEY


class CloudJob(models.Model):
    """Jobs with the agent that queued them, if any."""

    _inherit = "cloud.job"

    via_client = fields.Char(
        readonly=True,
        help="Set when the job was queued through the MCP endpoint, as in the "
        "audit log.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Stamp the jobs an agent's request queues; drop a forged stamp."""
        via = self.env.context.get(VIA_CONTEXT_KEY)
        for vals in vals_list:
            if via:
                vals["via_client"] = via
            elif not self.env.su:
                vals.pop("via_client", None)
        return super().create(vals_list)

    def write(self, vals):
        """Only the platform writes the stamp after the job exists."""
        if "via_client" in vals and not self.env.su:
            raise AccessError(
                self.env._("Only the platform marks a job as queued by an agent.")
            )
        return super().write(vals)
