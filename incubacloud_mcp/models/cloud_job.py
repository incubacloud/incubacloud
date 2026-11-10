"""A job queued by an agent says so, and how its call was approved."""

from odoo import api, fields, models
from odoo.tools.sql import create_index

from odoo.addons.incubacloud.models.cloud_security_mixin import defaults_as_sent

from .cloud_audit_log import APPROVAL_SELECTION, refuse_mark_write, stamp


class CloudJob(models.Model):
    """Jobs with the agent that queued them, if any, and how it was approved."""

    _inherit = "cloud.job"

    via_client = fields.Char(
        readonly=True,
        help="Set when the job was queued through the MCP endpoint, as in the "
        "audit log.",
    )
    approval = fields.Selection(
        APPROVAL_SELECTION,
        readonly=True,
        help="How the agent's call that queued the job was approved, as in the "
        "audit log.",
    )
    approval_rule_id = fields.Many2one(
        "incubacloud.mcp.auto.approval",
        readonly=True,
        index=True,
        ondelete="set null",
        help="The auto-approval rule that approved the call that queued the job.",
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
        """Stamp the jobs an agent's request queues; drop a forged stamp.

        A stamp among the caller's defaults is dropped too.
        """
        self = defaults_as_sent(self, vals_list)
        stamp(self.env, vals_list)
        return super().create(vals_list)

    def write(self, vals):
        """Only the platform writes the stamps after the job exists."""
        refuse_mark_write(
            self.env,
            vals,
            self.env._("Only the platform marks a job as queued by an agent."),
        )
        return super().write(vals)

    def _agent_info(self):
        """Return ``{job id: {via_client, approval}}``: what the lists show of
        the agent that queued each job and how its call was approved.

        :rtype: dict
        """
        return {
            job.id: {"via_client": job.via_client or "", "approval": job.approval or ""}
            for job in self
        }

    def _format(self):
        """Add the agent and the approval to each job the panel lists."""
        rows = super()._format()
        info = self._agent_info()
        for row in rows:
            row.update(info.get(row.get("id"), {}))
        return rows

    def _format_history(self):
        """Add the agent and the approval to each job of the history."""
        rows = super()._format_history()
        info = self._agent_info()
        for row in rows:
            row.update(info.get(row.get("id"), {}))
        return rows
