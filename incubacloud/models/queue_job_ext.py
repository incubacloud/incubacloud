import logging

from odoo import models

_logger = logging.getLogger(__name__)

# Terminal states that should sync cloud.job.state and notify the frontend.
_TERMINAL = frozenset(("done", "failed", "cancelled"))


class QueueJob(models.Model):
    """Extend queue.job to keep cloud.job.state in sync and notify the bus.

    queue_job uses two different cursors depending on the outcome:

    * Success  — _try_perform_job calls flush_all() + env.cr.commit()
      (main cursor).  We send the notification here so pg_notify is
      delivered after that same commit.

    * Failure  — _runjob opens a fresh cursor (new_cr), writes
      state='failed', and raises.  The main cursor is rolled back and
      flush_all() is never called on new_cr, so the stored related field
      cloud.job.state would stay stale.  We fix both problems here:
      explicit SQL UPDATE + bus send, all within new_cr so pg_notify fires
      when new_cr commits at the end of the `with` block.
    """

    _inherit = "queue.job"

    def write(self, vals):
        result = super().write(vals)
        new_state = vals.get("state")
        if new_state not in _TERMINAL:
            return result

        for qjob in self:
            cloud_jobs = self.env["cloud.job"].search(
                [("queue_job_uuid", "=", qjob.uuid)],
                limit=1,
            )
            if not cloud_jobs:
                continue

            cjob = cloud_jobs

            # Explicitly write cloud_job.state via SQL so it is visible
            # to the frontend even when flush_all() is never called
            # (failure path).
            self.env.cr.execute(
                "UPDATE cloud_job SET state = %s WHERE id = %s",
                (new_state, cjob.id),
            )
            _logger.info(
                "[queue_job_ext] uuid=%s state=%s -> cloud.job id=%s updated",
                qjob.uuid, new_state, cjob.id,
            )

            # When a job fails, cancel chained jobs stuck in
            # wait_dependencies for the same instance.
            if new_state == "failed" and cjob.instance_id:
                self.env.cr.execute(
                    "UPDATE cloud_job SET state = 'failed'"
                    " WHERE instance_id = %s"
                    "   AND state = 'wait_dependencies'"
                    "   AND id != %s",
                    (cjob.instance_id.id, cjob.id),
                )
                if self.env.cr.rowcount:
                    _logger.info(
                        "[queue_job_ext] cancelled %d chained"
                        " jobs for instance %s",
                        self.env.cr.rowcount,
                        cjob.instance_id.id,
                    )

            # Broadcast job update to all active internal users so every
            # user watching the cloud UI gets real-time notifications.
            # Also send email based on each user's notification preference.
            self.env['cloud.job']._broadcast_job_update(cjob.id)
            self.env['cloud.job']._notify_by_email(cjob, new_state)
            _logger.info(
                "[queue_job_ext] broadcast cloud.job id=%s state=%s",
                cjob.id, new_state,
            )

        return result
