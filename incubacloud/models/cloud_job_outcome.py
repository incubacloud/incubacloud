"""The mark a job's success hook leaves, so a repeated attempt runs nothing.

``on_success`` runs on a durable cursor that commits before the job's own
transaction (``AbstractExecutor._run_hook_durably``). When that
transaction then fails to commit — a lost serialization race on the store
of ``done`` (queue_job runs the job again five seconds later, outside
``max_retries``), or the worker dies before it — queue_job runs the job
again from the start, in a world where its work is already done: a
restore whose handoff its first ``on_success`` removed failed on the
missing archive and its move was rolled back (7-nov-2026), a rebuild
built and restarted a second time.

The row is inserted by that hook's cursor, as its last statement, only
when ``on_success`` returned: it commits with what the hook wrote, or not
at all. ``cloud.job.execute`` reads it before anything else and, when it
is there, ends the attempt without running anything again, after putting
back what the lost transaction held (``own_job_vals``) and arming again
what was left for its commit (``owed``).
"""
from odoo import fields, models


class CloudJobOutcome(models.Model):
    """A success hook of a job that committed, per queue_job attempt chain."""

    _name = "cloud.job.outcome"
    _description = "Job Outcome Committed"
    _order = "id desc"
    _log_access = False

    job_id = fields.Many2one(
        "cloud.job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    queue_job_uuid = fields.Char(
        required=True,
        index=True,
        help="The queue job whose attempts the mark answers for.",
    )
    own_job_vals = fields.Json(
        help=(
            "What the hook wrote on the job's own row through"
            " AbstractExecutor._write_own_job: in the job's transaction,"
            " so lost with it, and written again by the attempt that"
            " finds this mark."
        ),
    )
    owed = fields.Json(
        help=(
            "What the hook left for after the job's commit, which a"
            " rollback drops: a list of {'kind': ...} entries the"
            " attempt that finds this mark arms again"
            " (cloud.job._rearm_owed)."
        ),
    )
