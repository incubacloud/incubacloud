"""Where a job with a step run apart on its server keeps its place.

A step marked ``detached`` in ``get_commands()`` runs on the server in a
session of its own (``scripts/detached_step.sh``): it outlives the request
that started it, the worker that ran that request and a restart of the
panel. What the job knows of its run — which step comes next, what the
steps before it answered, how much of the running step's log is in the
job log already — must outlive them too, so the job can be followed,
stopped or closed by whoever comes next. That is this row.

One row per job, only for an executor whose plan has a step marked
``detached``. It is written on cursors of its own, never in the job's
transaction: queue_job rolls that back when an attempt ends in a retry,
and the job's row is locked by it (``AbstractExecutor._write_own_job``).
The log offset is written on the cursor that writes the log lines it
covers (``AbstractExecutor._flush_logs``), so an attempt that dies loses
no line and repeats none.

The rule: **while the row exists, the job may have something on a
server that nobody has closed.** It goes in the durable transaction of
the hook that closes the job, unless something was left alive that could
not be stopped, and when an attempt ends in a retry from the start. A row
kept that way is marked ``closed`` in that transaction: no attempt
resumes the job from it, and one that runs the job again stops and
removes what it says first. A
neutralized copy of the database has none (``data/neutralize.sql``): it
must not go stop the steps of the database it was copied from.

The plan and the steps' results are encrypted: a command, or what it
printed, can hold what must not be left readable in the database.
"""
import json

from odoo import fields, models

from .encrypted_char import EncryptedText


class CloudJobCheckpoint(models.Model):
    """The place of a job that runs steps apart on its server."""

    _name = "cloud.job.checkpoint"
    _description = "Job Checkpoint"
    _log_access = False

    job_id = fields.Many2one(
        "cloud.job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    queue_job_uuid = fields.Char(
        index=True,
        help="The queue job whose attempts wrote the row: a row of another"
             " one is not resumed, it is the reconciliation's.",
    )
    host_id = fields.Many2one(
        "cloud.host",
        ondelete="cascade",
        help="The job's host, where its steps run.",
    )
    next_index = fields.Integer(
        default=0,
        help="The first step of the plan that has not ended.",
    )
    detached_started = fields.Boolean(
        help="The step at next_index runs apart on the server.",
    )
    step_started_at = fields.Datetime(
        help="When the step at next_index started on the server.",
    )
    log_offset = fields.Integer(
        default=0,
        help="Bytes of that step's log already in the job log; written"
             " with the lines it covers.",
    )
    parked = fields.Boolean(
        help="The job waits between two attempts while its step runs.",
    )
    parked_until = fields.Datetime(
        help="When the parked job is to connect again.",
    )
    first_started_at = fields.Datetime(
        help="When the job's first attempt reached its steps.",
    )
    attempts = fields.Integer(
        default=0,
        help="Attempts that reached the steps.",
    )
    lost_contact_since = fields.Datetime(
        help="Since when the panel cannot reach the server of a running"
             " step.",
    )
    closed = fields.Boolean(
        help="A hook closed the job and kept the row: something it left on"
             " the server could not be stopped or removed. No attempt"
             " resumes the job from it; it is the reconciliation's, or the"
             " cleaning of an attempt that runs the job again.",
    )
    reconcile_tried_at = fields.Datetime(
        help="When the reconciliation last tried to reach the server to"
             " clean up after the ended job, and could not.",
    )
    build_lock_host_id = fields.Many2one(
        "cloud.host",
        ondelete="set null",
        help="The host whose build lock the parked job keeps.",
    )
    data = EncryptedText(
        help="JSON: the materialised plan ([label, command, opts]), the"
             " results of the steps that ended, the job's working"
             " directory, the secrets it staged and the executor's"
             " carried state.",
    )

    _job_uniq = models.Constraint(
        "unique(job_id)",
        "A job has one checkpoint.",
    )

    def _data(self):
        """Return the row's ``data`` as a dict, empty when there is none.

        :rtype: dict
        """
        self.ensure_one()
        return json.loads(self.data) if self.data else {}
