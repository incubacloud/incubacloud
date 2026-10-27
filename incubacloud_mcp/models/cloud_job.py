"""A job queued by an agent says so."""

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools.sql import create_index

from ..protocol.context import VIA_CONTEXT_KEY


class CloudJob(models.Model):
    """Jobs with the agent that queued them, if any."""

    _inherit = "cloud.job"

    via_client = fields.Char(
        readonly=True,
        help="Set when the job was queued through the MCP endpoint, as in the "
        "audit log.",
    )

    def init(self):
        """Index the few jobs that hold a secret.

        Every tool call reads the secrets of the database to mask them,
        and the jobs are its one big table: without this, each call
        would scan all of them.
        """
        super().init()
        create_index(
            self.env.cr,
            "cloud_job_secret_payload_set_index",
            self._table,
            ["id"],
            where="secret_payload IS NOT NULL AND secret_payload <> ''",
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
