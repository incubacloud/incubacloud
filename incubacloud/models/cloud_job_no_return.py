"""The mark a running job leaves once it starts a step it cannot undo.

Some steps cannot be stopped halfway: «Update changed modules» of a
rebuild commits each module it updates (Odoo's ``loading.py``), so a stop
there leaves the database half updated under the image that runs, and
the rebuild keeps no copy to go back to (measured on 8-nov-2026). Such a
step is the executor's ``_no_return_from``.

The stop and the step are serialised by a transaction-level advisory
lock of the job (``NO_RETURN_LOCK_NAMESPACE``, the job's id):

* The executor, on a durable cursor of its own, takes the lock, reads the
  stop flag and, without it, inserts this mark and commits before the
  step runs (``AbstractExecutor._pass_no_return``). With the flag, the
  step does not start.
* Whoever writes the flag on a started job takes the same lock and, with
  the mark there, refuses (``cloud.job._refuse_stop_past_no_return``), as
  ``cancel_job`` does for a job in any state. A caller about to discard
  the instance forces the stop with ``ic_stop_past_no_return``
  (``cloud.job.write``).

The mark is a row of its own and not a column of ``cloud_job``: the
job's transaction stores ``done`` on that row at the end, and a write of
it committed by another cursor meanwhile would make that store fail to
serialize (``AbstractExecutor._write_own_job``). Nor is it a kind of
``cloud.job.outcome``, whose presence means the success hook committed
and the next attempt runs nothing: every reader of it would have to tell
the two apart.

The mark is the instance's, not an attempt's: a database updated stays
ahead whichever attempt updated it. A new attempt (a postpone, a worker
that died) keeps it, and a stop between two attempts is refused too. It
goes once the job ends done or cancelled, after that end commits
(``queue.job._forget_no_return_after_commit``); a failed job keeps it,
as queue_job may be told to run it again.
"""
from odoo import fields, models

#: The namespace of the advisory lock of a job's stop, next to the queues'
#: (``cloud.job``) and the doors' (``cloud.restore.upload.grant``).
NO_RETURN_LOCK_NAMESPACE = 0x0C10D10E


class CloudJobNoReturn(models.Model):
    """A started job that is past a step it cannot be stopped in."""

    _name = "cloud.job.no.return"
    _description = "Job Past Its Point of No Return"
    _log_access = False

    job_id = fields.Many2one(
        "cloud.job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    step = fields.Char(
        required=True,
        help="The label of the step the job had started, for the refusal.",
    )

    _job_uniq = models.Constraint(
        "unique(job_id)",
        "A job is past its point of no return once.",
    )
