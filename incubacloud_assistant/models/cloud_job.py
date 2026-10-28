"""A job the assistant waits for wakes its conversation when it ends."""

from odoo import models


class CloudJob(models.Model):
    """The end of a job resumes the conversations waiting for it."""

    _inherit = "cloud.job"

    def _after_job_terminal(self, new_state):
        """Resume the assistant's conversations that waited for this job.

        In the transaction that records the end and inside the savepoint of
        ``_run_after_terminal``: a failure here is logged and never changes
        how the job ended.
        """
        super()._after_job_terminal(new_state)
        waiting = (
            self.env["incubacloud.assistant.conversation"]
            .sudo()
            .search([("waiting_job_id", "in", self.ids), ("state", "=", "waiting_job")])
        )
        Engine = self.env["incubacloud.assistant.engine"]
        for conversation in waiting:
            Engine._job_ended(conversation, conversation.waiting_job_id, new_state)
