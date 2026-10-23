"""Post-migrate for 1.0.198 — the job queue per instance.

``cloud.job.state`` is the job's own field now, with ``queued`` for a
job waiting in its target's queue. Two things of the old shape are
settled here:

* A job whose queue.job was deleted before 1.0.90 cut the link first
  has an empty state (29 in production, July–August 2026): not waiting
  for an alert, with no queue.job, it is nothing the queue can serve.
  It ends ``cancelled``, with a note saying why.
* The pushes that arrived during a rebuild were kept in
  ``cloud.instance.pending.push`` and folded into one rebuild when it
  ended. A push now merges into the rebuild waiting in the instance's
  queue, so the model and its table go. Nothing is migrated from it: a
  row there described a rebuild that the next push or rebuild would
  have folded in anyway.

The sweep cron of the queues (``cron_job_queue_dispatch``) arrives with
this version; on an existing database it lands owned by uid 1, so it is
put on the platform bot like every other cron of the module.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_NOTE = "Cancelled: no queue.job when the job queue was introduced."


def migrate(cr, version):
    """Settle the stateless jobs, drop the pending-push table, enrol the cron."""
    cr.execute(
        """
        UPDATE cloud_job
           SET state = 'cancelled',
               write_date = (now() at time zone 'UTC')
         WHERE state IS NULL
           AND queue_job_id IS NULL
           AND blocked_alert_id IS NULL
        RETURNING id
        """
    )
    job_ids = [row[0] for row in cr.fetchall()]
    _logger.info(
        "cloud.job: %d job(s) with no queue.job and no state cancelled",
        len(job_ids),
    )
    if job_ids:
        cr.execute(
            """
            INSERT INTO cloud_job_log_chunk
                (job_id, content, source, create_date, write_date)
            SELECT id, %s, 'system',
                   (now() at time zone 'UTC'), (now() at time zone 'UTC')
              FROM cloud_job
             WHERE id IN %s
            """,
            (_NOTE, tuple(job_ids)),
        )
    cr.execute("DROP TABLE IF EXISTS cloud_instance_pending_push CASCADE")
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
