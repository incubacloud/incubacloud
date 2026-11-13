import logging

from psycopg2 import OperationalError

from odoo import _, fields, models

from odoo.addons.queue_job.controllers.main import RunJobController
from odoo.addons.queue_job.job import Job

from ._concurrency import run_after_commit
from .abstract_executor import (
    BLOCKED_BY_ALERT,
    CANCELLED_WHILE_RUNNING,
    PARKED_RETURN_PRIORITY,
    deferred_instance_unlink,
)
from .purge_archived_executor import PurgeArchivedBackupsExecutor
from .registry import executor_registry

_logger = logging.getLogger(__name__)

# Terminal states that notify the frontend and move the target's queue on.
_TERMINAL = frozenset(("done", "failed", "cancelled"))
# Every state the runner writes through the ORM, mirrored on cloud.job.
_MIRRORED = _TERMINAL | frozenset(("pending", "started", "wait_dependencies"))


class QueueJob(models.Model):
    """Extend queue.job to keep cloud.job.state in sync and notify the bus.

    queue_job uses two different cursors depending on the outcome:

    * Success  — _try_perform_job calls flush_all() + env.cr.commit()
      (main cursor).  We send the notification here so pg_notify is
      delivered after that same commit.

    * Failure  — _runjob opens a fresh cursor (new_cr), writes
      state='failed', and raises.  The main cursor is rolled back; the
      mirror and the bus send happen within new_cr, which flushes and
      commits at the end of the `with` block, so pg_notify fires then.
    """

    _inherit = "queue.job"

    def unlink(self):
        """Delete queue jobs without erasing the history of their cloud jobs.

        ``cloud.job.date_done`` is a stored related field on
        ``queue_job_id`` (``state`` was one too). Deleting the queue job
        makes the ORM recompute it through a link that no longer exists,
        so it is written back empty. The autovacuum deletes queue jobs
        after 30 days, while cloud jobs are kept for the instance
        timeline for 180: every job in that window lost its end date.

        The link is cut first, with SQL, so no cloud job depends on the
        records being deleted and nothing is recomputed. The stored values
        stay as they were — the last state the job reached.
        """
        if self.ids:
            # Store first what is still pending: a write of the link would
            # land after the UPDATE and restore it, and a related value not
            # computed yet would be computed after it, from an empty link.
            self.env["cloud.job"].flush_model(
                ["queue_job_id", "state", "date_done"],
            )
            self.env.cr.execute(
                "UPDATE cloud_job SET queue_job_id = NULL"
                " WHERE queue_job_id IN %s",
                (tuple(self.ids),),
            )
            if self.env.cr.rowcount:
                self.env["cloud.job"].invalidate_model(["queue_job_id"])
        return super().unlink()

    def _message_post_on_failure(self):
        """Leave the platform's jobs out of queue_job's failure messages.

        queue_job posts a "job failed" message on the job and subscribes
        every Job Queue Manager and the job's creator, so each of them is
        mailed. A ``cloud.job`` failure already has its channel —
        ``job_failed`` and its siblings, which follow each user's level,
        muted projects and digest — so that mail was the same failure a
        second time, outside all of it, and to the cron bot, which has no
        address (16-oct-2026). Jobs that are not the platform's keep
        queue_job's behaviour.
        """
        if not self.ids:
            return super()._message_post_on_failure()
        self.env.cr.execute(
            "SELECT q.id FROM queue_job q"
            " JOIN cloud_job c ON c.queue_job_uuid = q.uuid"
            " WHERE q.id IN %s",
            (tuple(self.ids),),
        )
        ours = self.browse([row[0] for row in self.env.cr.fetchall()])
        rest = self - ours
        if rest:
            return super(QueueJob, rest)._message_post_on_failure()
        return None

    def _cancelled_while_running(self):
        """The jobs of ``self`` whose cloud job a person cancelled mid-run.

        queue_job cannot stop a started job, so ``cloud.job.cancel_job``
        marks the cloud job ``cancelled`` and the executor stops on that
        flag (``JobCancelled``). ``cloud.job.execute`` returns
        ``CANCELLED_WHILE_RUNNING`` then, which queue_job stores as done;
        a stop that still arrives as an exception is stored as a failure.
        The flag is read from the row, not the cache: another transaction
        wrote it while this job ran.
        """
        if not self.ids:
            return self.browse()
        self.env.cr.execute(
            "SELECT q.id FROM queue_job q"
            " JOIN cloud_job c ON c.queue_job_uuid = q.uuid"
            " WHERE q.id IN %s AND c.state = 'cancelled'",
            (tuple(self.ids),),
        )
        return self.browse([row[0] for row in self.env.cr.fetchall()])

    def _parked_ended_by_hand(self, vals):
        """The jobs of ``self`` ended from queue_job's own views while parked.

        A parked job's queue.job waits ``pending`` for its next attempt;
        ended from there — cancelled, set done or failed by hand — it is
        not a job that never ran: its step may run on the server, which
        only an attempt of it or the reconciliation can reach. Its cloud
        job is left ``started`` for the reconciliation, which stops the
        step and gives the job its hook (``cloud.job._clean_up_lost_job``).
        Any row of the queue job counts, parked or not: one whose worker
        died mid-attempt is put back ``pending`` by queue_job's SQL, with
        the row as that attempt left it.

        :param dict vals: what is being stored
        :return: the ``queue.job`` records concerned
        """
        if vals.get("state") not in _TERMINAL or not self.ids:
            return self.browse()
        waiting = self.filtered(lambda job: job.state not in _TERMINAL | {"started"})
        if not waiting:
            return self.browse()
        return waiting._with_parked_checkpoint(parked_only=False)

    def _with_parked_checkpoint(self, parked_only=True):
        """The jobs of ``self`` whose cloud job's checkpoint is ``parked``.

        Written by the executor's attempt on a cursor of its own, and read
        on a fresh cursor: the reader's transaction may predate it.

        :param bool parked_only: only a row marked ``parked``; False for
            any row this queue job wrote
        :return: the ``queue.job`` records concerned
        """
        with self.env["cloud.job"]._fresh_env() as env:
            env.cr.execute(
                "SELECT q.id FROM queue_job q"
                " JOIN cloud_job c ON c.queue_job_uuid = q.uuid"
                " JOIN cloud_job_checkpoint k"
                "   ON k.job_id = c.id AND k.queue_job_uuid = q.uuid"
                " WHERE q.id IN %s AND (k.parked OR NOT %s)",
                (tuple(self.ids), parked_only),
            )
            ids = [row[0] for row in env.cr.fetchall()]
        return self.browse(ids)

    def _parked_postpones(self, vals):
        """The jobs of ``self`` whose attempt parked, as their postpone is stored.

        queue_job stores ``pending`` over ``started`` once an attempt raised
        ``JobParked``: its step keeps running on the server, and the job
        goes on in a later attempt. The executor marked the job's checkpoint
        ``parked`` before it raised, on a cursor of its own: read on a
        fresh cursor, as the runner's transaction began before that commit.

        :param dict vals: what queue_job is storing
        :return: the ``queue.job`` records concerned
        """
        if vals.get("state") != "pending" or not self.ids:
            return self.browse()
        started = self.filtered(lambda job: job.state == "started")
        if not started:
            return self.browse()
        return started._with_parked_checkpoint()

    def _succeeded_before_cancel(self, outcome=False):
        """The jobs of ``self`` whose ``on_success`` returned before a stop.

        ``AbstractExecutor._mark_success_before_cancel`` records it on the
        cloud job, on a cursor of its own, when that job was already
        cancelled; with *outcome*, the mark every ``on_success`` leaves on
        its cursor counts too (``cloud.job.outcome``). Both are read on a
        fresh cursor (``cloud.job._fresh_env``): the store of ``done``
        reaches here in the runner's transaction, whose snapshot predates
        that commit and would never see either.

        :param bool outcome: count a job whose success hook committed
        :return: the ``queue.job`` records concerned
        """
        if not self.ids:
            return self.browse()
        committed = (
            " OR EXISTS (SELECT 1 FROM cloud_job_outcome o"
            " WHERE o.job_id = c.id AND o.queue_job_uuid = q.uuid)"
            if outcome else ""
        )
        with self.env["cloud.job"]._fresh_env() as env:
            env.cr.execute(
                "SELECT q.id FROM queue_job q"
                " JOIN cloud_job c ON c.queue_job_uuid = q.uuid"
                " WHERE q.id IN %s AND (c.succeeded_before_cancel" + committed + ")",
                (tuple(self.ids),),
            )
            ids = [row[0] for row in env.cr.fetchall()]
        return self.browse(ids)

    def _committed_own_job_vals(self):
        """What the committed success of each job of ``self`` wrote on its row.

        ``AbstractExecutor._write_own_job`` writes in the job's
        transaction; the outcome mark keeps a copy, which outlives a
        transaction rolled back. Read on a fresh cursor, as the mark.

        :return: ``{cloud.job id: values}`` for the jobs that wrote some
        :rtype: dict
        """
        if not self.ids:
            return {}
        with self.env["cloud.job"]._fresh_env() as env:
            env.cr.execute(
                "SELECT DISTINCT ON (o.job_id) o.job_id, o.own_job_vals"
                " FROM cloud_job_outcome o"
                " JOIN queue_job q ON q.uuid = o.queue_job_uuid"
                " WHERE q.id IN %s ORDER BY o.job_id, o.id DESC",
                (tuple(self.ids),),
            )
            return {job_id: vals for job_id, vals in env.cr.fetchall() if vals}

    def _store_success_before_cancel(self, vals):
        """Store ``self`` done, whatever end queue_job was storing.

        queue_job was putting it back in the queue (the store of ``done``
        failed to serialize against the stop's write) or storing a
        failure that came after the success. Neither path lets queue_job
        start the steps that wait on it, so they are set going here, as
        ``button_done`` sets them going, with the retries queue_job gives
        that same update (``_enqueue_dependent_jobs``).

        Either way the job's own transaction was rolled back, and with it
        the unlink a delete's or a purge's ``on_success`` left for its
        commit (``AbstractExecutor._unlink_after_job_commit``): the job
        would end done with its instance still in the panel. The unlink
        is armed again here, for this cursor's commit, and what the hook
        wrote on the job's own row is written again from its outcome mark.

        Also for a job whose ``on_success`` had committed (the mark,
        ``cloud.job.outcome``) stored failed — what failed came after its
        outcome — or put back in the queue: then this runs on the
        temporary cursor of queue_job's ``retry_postpone``, and what it
        arms for after the commit runs when that cursor commits. And for
        the reconciliation's close of a lost worker whose success was
        saved (``cloud.job._close_lost_job``).

        :param dict vals: what queue_job was writing
        :return: the result of ``write``
        """
        done = {
            key: value for key, value in vals.items()
            # The run's own start, which a postpone clears.
            if key not in ("date_started", "exec_time")
        } | {
            "state": "done",
            "date_done": fields.Datetime.now(),
            "date_cancelled": False,
            "eta": False,
            "result": False,
            "exc_name": False,
            "exc_message": False,
            "exc_info": False,
        }
        result = self.write(done)
        self.flush_recordset(["state"])
        self._unlink_owed_instances()
        for job_id, own in self._committed_own_job_vals().items():
            job = self.env["cloud.job"].sudo().browse(job_id)
            job.write(own)
            job.flush_recordset(list(own))
        for qjob in self:
            if not (qjob.dependencies or {}).get("reverse_depends_on"):
                continue
            try:
                # Its own savepoint and retries, as after any done.
                RunJobController._enqueue_dependent_jobs(
                    self.env, Job.load(self.env, qjob.uuid),
                )
            except OperationalError:
                # The tries ran out. Logged, not raised: the job stays
                # done, as queue_job leaves it when the same update fails
                # after its commit.
                _logger.exception(
                    "[queue_job_ext] queue job %s: the steps waiting on it"
                    " could not be set going", qjob.uuid,
                )
        _logger.info(
            "[queue_job_ext] queue jobs %s succeeded before what ended them: done",
            self.ids,
        )
        return result

    def _unlink_owed_instances(self):
        """Arm the unlink of the instances the jobs of ``self`` removed.

        A delete's ``on_success`` marks its instance ``removal_finalized``
        and a purge's leaves it archived; both leave the unlink for the
        job's commit. Called once ``on_success`` returned on each of them
        (``_store_success_before_cancel``, and an attempt that finds its
        outcome saved, ``cloud.job._rearm_owed``), so what is left of
        their instance only awaits that unlink. Registered on this
        cursor's ``postcommit``, as superuser, as the executor registers
        it.

        The instance is read on a fresh cursor (``cloud.job._fresh_env``):
        the store of ``done`` calls this in the runner's transaction,
        whose snapshot predates the hook's commit and still shows the
        instance as it was before it.
        """
        owed = []
        with self.env["cloud.job"]._fresh_env() as env:
            for qjob in self:
                cjob = env["cloud.job"].with_context(
                    active_test=False,
                ).search([("queue_job_uuid", "=", qjob.uuid)], limit=1)
                inst = cjob.instance_id.with_context(active_test=False)
                if not inst.exists():
                    continue
                purged = (
                    cjob.job_type_id.code == PurgeArchivedBackupsExecutor._job_type
                    and not inst.active
                )
                if inst.removal_finalized or purged:
                    owed.append((cjob.id, inst.id, cjob.host_id.id))
        Job = self.env["cloud.job"].sudo()
        for job_id, inst_id, host_id in owed:
            inst = self.env["cloud.instance"].sudo().with_context(
                active_test=False,
            ).browse(inst_id)
            self.env.cr.postcommit.add(deferred_instance_unlink(
                Job.env, inst, job_id, host_id, sudo=True,
            ))

    def write(self, vals):
        # A job a person cancelled while it ran ends cancelled, as one
        # cancelled before it started does: queue_job stores the stop as
        # a failure, which raised a failure alert and mailed the customer
        # that a job they had stopped themselves had failed (12-oct-2026).
        # ``execute`` returns the stop rather than raising it, so queue_job
        # logs no traceback for it; that arrives here as "done" with the
        # marker. A job that finished on its own as the person pressed
        # cancel carries no marker and stays done.
        #
        # A started job queue_job puts back in the queue is the same stop.
        # Storing its end updates the cloud job's row, which the flag
        # changed after the job's transaction began: PostgreSQL refuses
        # it, queue_job retries the whole job five seconds later, and the
        # retry wrote ``started`` over the flag and ran the job again
        # from the start. No cancellation of a running job had ever ended
        # cancelled (16-oct-2026); the postpone is read in a cursor of its
        # own, which sees the flag.
        #
        # A job that parked is still running: its step on the server, and
        # the job in its next attempt, which comes back first of its
        # channel (``PARKED_RETURN_PRIORITY``). Its cloud job stays
        # ``started``, and the parking does not count as an attempt that
        # failed: queue_job's ``JobFoundDead`` then means three deaths in a
        # row, with no clean attempt between them. A stop written while it
        # parked is not stored as one either: the job comes back and closes
        # itself.
        parked = self._parked_postpones(vals)
        if parked:
            (self - parked).write(vals)
            return super(QueueJob, parked).write(vals | {
                "priority": PARKED_RETURN_PRIORITY,
                "retry": 0,
            })
        # A postpone that is not a park: the job sleeps until its eta,
        # and its slot in the channel is free for the jobs held for one.
        postponed = (
            self.filtered(lambda job: job.state == "started")
            if vals.get("state") == "pending" else self.browse()
        )
        ended = self._parked_ended_by_hand(vals)
        if ended:
            (self - ended).write(vals)
            _logger.info(
                "[queue_job_ext] queue jobs %s ended by hand while parked:"
                " left to the reconciliation", ended.ids,
            )
            return super(QueueJob, ended).write(vals)
        if vals.get("state") == "failed" or (
            vals.get("state") == "done"
            and vals.get("result") == CANCELLED_WHILE_RUNNING
        ):
            candidates = self
        elif vals.get("state") == "pending":
            candidates = self.filtered(lambda job: job.state == "started")
        else:
            candidates = self.browse()
        if candidates:
            # Its ``on_success`` had returned when the stop arrived: the
            # job is done, and its chain goes on. Stored cancelled, a
            # cutover that had cut over cancelled the freeze of its
            # source; put back in the queue, it would run again. A job
            # whose success hook committed (its outcome mark) is done too,
            # however it was being stored: failed, with the stop's result,
            # or put back in the queue — the store of done lost its race,
            # against a stop that came after the mark or anything else.
            # Read as the stop only, a stop after the mark ended it
            # cancelled and cut its chain.
            succeeded = candidates._succeeded_before_cancel(outcome=True)
            if succeeded:
                (self - succeeded).write(vals)
                return succeeded._store_success_before_cancel(vals)
            stopped = candidates._cancelled_while_running()
            if stopped:
                (self - stopped).write(vals)
                result = stopped.with_context(
                    ic_cancelled_while_running=True,
                ).write(vals | {
                    "state": "cancelled",
                    "date_cancelled": fields.Datetime.now(),
                    "date_done": False,
                    "result": CANCELLED_WHILE_RUNNING,
                    "exc_name": False,
                    "exc_message": False,
                    "exc_info": False,
                })
                stopped._let_go_between_ticks(vals)
                return result
        result = super().write(vals)
        if postponed:
            self.env["cloud.job"]._wake_channel_waiters()
        new_state = vals.get("state")
        if new_state not in _MIRRORED:
            return result

        kept_cancelled = self._started_again_after_a_stop(new_state)
        for qjob in self:
            cloud_jobs = self.env["cloud.job"].search(
                [("queue_job_uuid", "=", qjob.uuid)],
                limit=1,
            )
            if not cloud_jobs:
                continue

            cjob = cloud_jobs
            if qjob in kept_cancelled:
                _logger.info(
                    "[queue_job_ext] uuid=%s started again after a stop:"
                    " cloud.job id=%s stays cancelled", qjob.uuid, cjob.id,
                )
                continue

            # The cloud job's own ``state`` follows the queue.job's on
            # every transition the runner makes through the ORM:
            # ``started`` and ``done`` in the job's transaction, which
            # commits right after; ``failed`` and a postponed
            # ``pending`` on the runner's cursor of their own, which
            # commit when it closes (a commit flushes). ``enqueued`` is
            # written by SQL and never arrives here: a dispatched job
            # shows ``pending`` until it starts. Written as the platform
            # — the operator who cancels a queued job has no write on
            # queue jobs — and without the state hook: the end is acted
            # on below, once its alerts are recorded.
            mirror = {"state": new_state} | self._new_attempt_clears(
                cjob, new_state,
            )
            # An earlier attempt's point of no return stays with a new
            # one: the database it updated is ahead all the same
            # (``cloud.job.no.return``).
            cjob.sudo().with_context(ic_skip_state_hook=True).write(mirror)
            # To the row now: ``_check_cancel`` and the stop detection
            # above read it with SQL, from other transactions and from
            # this one.
            cjob.flush_recordset(["state"])
            if new_state == "started":
                qjob._note_return_wait(cjob)
            _logger.info(
                "[queue_job_ext] uuid=%s state=%s -> cloud.job id=%s updated",
                qjob.uuid, new_state, cjob.id,
            )
            if new_state not in _TERMINAL:
                continue
            if new_state in ("done", "cancelled"):
                self._forget_no_return_after_commit(cjob)

            # Cancelled dispatched before it started (``cancel_job`` →
            # ``button_cancelled``): it never runs, so its executor lets
            # go of what was left on a host for it. One stopped while it
            # ran arrives with ``ic_cancelled_while_running``.
            if new_state == "cancelled" and not self.env.context.get(
                "ic_cancelled_while_running",
            ):
                self._let_go_for_jobs_never_run(cjob)

            # A job that failed or was cancelled — stopped while it ran,
            # or cancelled dispatched before it started (``cancel_job`` →
            # ``button_cancelled``, whose own cancel of the next steps is
            # SQL nothing mirrors) — ends its chain: what waits on it
            # would otherwise wait forever, holding its targets' slots.
            if new_state in ("failed", "cancelled") and qjob.graph_uuid:
                self._end_chain_after(cjob, qjob)

            # Broadcast job update to all active internal users so every
            # user watching the cloud UI gets real-time notifications.
            # External notifications (email, Telegram, webhook) for
            # failures are handled by cloud.alert._dispatch_notifications()
            # so there is a single, unified code path. For successful
            # completion we still notify directly because no alert is
            # created for done/cancelled; ``cancelled`` is announced to
            # nobody (``_notify_by_email``).
            blocked = qjob.result == BLOCKED_BY_ALERT
            if new_state == 'done':
                # The runner's store of ``done`` writes nothing but the
                # job's own rows (``_announce_done_after_commit``).
                self._announce_done_after_commit(cjob, blocked)
            else:
                self.env['cloud.job']._broadcast_job_update(
                    cjob.id, state=new_state,
                )
            # A job that ended blocked did nothing: there is no success to
            # announce, no failure to dismiss and nobody's activity to
            # count. The alert it waits for is what people are told about.
            if blocked:
                continue
            _logger.info(
                "[queue_job_ext] broadcast cloud.job id=%s state=%s",
                cjob.id, new_state,
            )

            # Persist a cloud.alert on failure / dismiss any leftover
            # alert when the same job succeeds on retry. These live in
            # the Alerts panel so operators see failures after the
            # real-time toast has disappeared.
            CJob = self.env['cloud.job']
            if new_state == 'failed':
                # A monitoring probe that burned its whole retry budget
                # against an unreachable host is the one failure that
                # means something about the *host* rather than the job.
                # It is raised here, on the terminal state, because
                # that is the only place where "it gave up" is a fact
                # rather than a prediction.
                CJob._create_host_unreachable_alert(cjob, qjob)
                CJob._create_job_failed_alert(
                    cjob, exc_message=qjob.exc_message,
                )
            elif new_state == 'done':
                self._dismiss_failures_after_commit(cjob)
                self._seal_autopurge_clock(cjob)
            # The end is acted on last, with its alerts recorded: a
            # failure that holds the queue has its alert, and a retry
            # that succeeded has dismissed it, before the next entry of
            # the target's queue is looked at — once this transaction
            # commits, on a cursor of its own. Nothing raised there, or
            # by a layer's hook, reaches the runner's ``store()``. For
            # ``done`` the layers' hooks wait for that commit too.
            cjob._on_state_changed(new_state, after_commit=new_state == 'done')

        return result

    def _end_chain_after(self, cjob, qjob):
        """Cancel the steps of *qjob*'s chain still waiting after *cjob*.

        A chain is one queue_job graph, its steps created and chained in
        the order of their ids (``cloud.job._dispatch_chain``): what waits
        on the ended job is what comes after it in its graph — a waiting
        step cancelled by hand ends the steps after it, not those before,
        and a job outside the chain on the same instance (a probe) ends
        nothing of it. Those steps never ran: they end ``cancelled``
        whether the job before them failed or was cancelled — only the
        job that failed is a failure, with its alert, and a step left
        ``failed`` would offer a retry that runs it out of its chain (a
        move's teardown of the source, after a failed cutover). Their
        queue.jobs are cancelled too, or a worker would still pick them
        up.

        Each step gets a note in its log, the layers hear of it
        (``_after_job_terminal("cancelled")``, never raising), and the
        queues the steps occupied are served once this transaction
        commits. Not their executor's ``on_cancelled_queued``: that undoes
        what was written when the chain was asked for, which after a
        partial run is what recovering from it needs (a move's markers,
        which ``rollback_move`` reads). Their ``on_never_run`` instead,
        which lets go of what the steps that ran left on a host for them
        (a restore's handoff).

        :param cjob: the ``cloud.job`` that just failed or was cancelled
        :param qjob: its ``queue.job``, in a graph
        """
        self.env.cr.execute(
            "UPDATE cloud_job c SET state = 'cancelled',"
            " write_date = (now() at time zone 'UTC')"
            " FROM queue_job q"
            " WHERE c.queue_job_id = q.id"
            "   AND q.graph_uuid = %s"
            "   AND c.state = 'wait_dependencies'"
            "   AND c.id > %s"
            " RETURNING c.id",
            (qjob.graph_uuid, cjob.id),
        )
        Job = self.env['cloud.job'].sudo()
        steps = Job.browse([row[0] for row in self.env.cr.fetchall()])
        self.env.cr.execute(
            "UPDATE queue_job q SET state = 'cancelled',"
            " date_cancelled = (now() at time zone 'UTC')"
            " FROM cloud_job c"
            " WHERE c.queue_job_id = q.id"
            "   AND q.graph_uuid = %s"
            "   AND q.state = 'wait_dependencies'"
            "   AND c.id > %s",
            (qjob.graph_uuid, cjob.id),
        )
        if self.env.cr.rowcount:
            self.invalidate_model(['state', 'date_cancelled'])
        if not steps:
            return
        Job.invalidate_model(['state'])
        _logger.info(
            "[queue_job_ext] cancelled %d chained jobs after cloud.job %s",
            len(steps), cjob.id,
        )
        if cjob.state == 'failed':
            note = _("Not run: step #%(step)s ended failed.", step=cjob.id)
        else:
            note = _("Not run: step #%(step)s ended cancelled.", step=cjob.id)
        self.env['cloud.job.log.chunk'].sudo().create([
            {"job_id": step.id, "content": note, "source": "system"}
            for step in steps
        ])
        steps._run_after_terminal('cancelled')
        self._let_go_for_jobs_never_run(steps)
        Job._schedule_dispatch(Job._queue_scopes_of(steps))

    def _started_again_after_a_stop(self, new_state):
        """The jobs of ``self`` that start again although a person stopped them.

        A job cancelled while it ran whose worker died: queue_job puts it
        back in the queue with SQL, and its cloud job keeps the flag. The
        mirror wrote ``started`` over it, so the job ran again from the
        start with no hook at all (6-nov-2026). Their cloud jobs keep
        ``cancelled``, and what they carry with it; ``cloud.job.execute``
        reads the flag before anything runs and closes the job as a stop.

        :param str new_state: the state being stored
        :return: the ``queue.job`` records whose mirror is skipped
        """
        if new_state != "started":
            return self.browse()
        return self._cancelled_while_running()

    @staticmethod
    def _new_attempt_clears(cjob, new_state):
        """Return what a new attempt of *cjob* clears as it starts.

        In the transaction that commits before it runs: what an earlier
        attempt recorded is not its own — a success before a stop, the
        reconciliation's claim and the sweep's first sight of a dead stop
        (a job requeued by hand after them, whose next death is a new
        one, alerted on from its own sight). Nothing for another state.

        :param cjob: the ``cloud.job`` whose mirror is written
        :param str new_state: the state being mirrored
        :return: values for its mirror's write
        :rtype: dict
        """
        if new_state != "started":
            return {}
        return {
            name: False
            for name in (
                "succeeded_before_cancel", "worker_lost_at",
                "found_dead_seen_at",
            )
            if cjob[name]
        }

    def _late_return_s(self, cjob):
        """Return how late *cjob* comes back from a park, in seconds.

        Past the ``parked_until`` of its checkpoint, written by this queue
        job's attempt that parked; 0 for a start that is no return from a
        park, or one before it was due. Read in the transaction that stores
        ``started``, which began after the park committed: the runner
        starts the job once its ``eta`` passed, which queue_job stored
        after the park.

        :param cjob: the ``cloud.job`` of this queue job
        :rtype: int
        """
        self.ensure_one()
        self.env.cr.execute(
            "SELECT parked_until FROM cloud_job_checkpoint"
            " WHERE job_id = %s AND queue_job_uuid = %s AND parked"
            "   AND parked_until IS NOT NULL",
            (cjob.id, self.uuid),
        )
        row = self.env.cr.fetchone()
        if not row:
            return 0
        return max(0, int((fields.Datetime.now() - row[0]).total_seconds()))

    def _note_return_wait(self, cjob):
        """Add to *cjob*'s ``return_wait_s`` how late this attempt starts.

        In the transaction that stores ``started``, never from the job's
        run, which holds its row; in a savepoint, so a measure that fails
        never keeps the job from starting. Nothing for a hidden type.

        :param cjob: the ``cloud.job`` of this queue job
        """
        self.ensure_one()
        if cjob.sudo().job_type_id.code in cjob._get_hidden_job_types():
            return
        try:
            # SQL only inside: nothing of the ORM to flush, or to lose.
            with self.env.cr.savepoint(flush=False):
                late = self._late_return_s(cjob)
                self.env.cr.execute(
                    "UPDATE cloud_job SET return_wait_s ="
                    " COALESCE(return_wait_s, 0) + %s WHERE id = %s",
                    (late, cjob.id),
                )
        except Exception:  # noqa: BLE001 — a measure, never the start
            _logger.warning(
                "[queue_job_ext] cloud.job id=%s: its late return was not"
                " measured", cjob.id, exc_info=True,
            )
        cjob.invalidate_recordset(["return_wait_s"])

    def _let_go_between_ticks(self, vals):
        """Give ``on_never_run`` to the jobs of ``self`` stopped between ticks.

        Called once what queue_job was storing on each, *vals*, was
        stored as the stop it was; only a postpone (``pending``) counts.
        A job that runs in ticks (``_postpones_between_ticks``) holds
        between two of them what it had begun, and a stop committed after
        the tick read the flag reached no hook: its ``on_never_run`` lets
        go, as for one cancelled while it waited for its next tick. Other
        jobs are left as they are. Never raises.

        :param dict vals: what queue_job was writing
        """
        if vals.get("state") != "pending":
            return
        ticking = self.env["cloud.job"].sudo().search(
            [("queue_job_uuid", "in", self.mapped("uuid"))],
        ).filtered(lambda job: getattr(
            executor_registry.get(job.job_type_id.code),
            "_postpones_between_ticks", False,
        ))
        if ticking:
            self._let_go_for_jobs_never_run(ticking)

    def _let_go_for_jobs_never_run(self, jobs):
        """Give each of *jobs*' executors its ``on_never_run``; never raises.

        In a savepoint, logged: the end of a job stands whatever the hook
        does, as with ``on_cancelled_queued``.

        :param jobs: ``cloud.job`` records that will never run
        """
        for job in jobs.sudo():
            executor_cls = executor_registry.get(job.job_type_id.code)
            if not executor_cls:
                continue
            try:
                with self.env.cr.savepoint():
                    executor_cls(
                        job_record=job, host_record=job.host_id,
                    ).on_never_run()
            except Exception:  # the end stands
                _logger.warning(
                    "Job %s: on_never_run raised.", job.id, exc_info=True,
                )

    def _forget_no_return_after_commit(self, cjob):
        """Delete *cjob*'s point-of-no-return mark once its end commits.

        A job done or cancelled never runs again, and its mark has been
        read by then: the terminal hooks that say what was left
        (``_warn_unapplied``) ran before the end was stored. A failed job
        keeps it: queue_job may be told to run it again (Requeue), over a
        database it left ahead. After the commit, on a cursor of its own:
        the mark was committed by the executor after this transaction's
        snapshot, which does not see it, and a store that then fails (a
        lost serialization race, the job runs again) drops the delete
        with it. A worker that dies in between leaves the row, which
        harms nothing and goes with the job.

        :param cjob: the ``cloud.job`` stored done or cancelled
        """
        registry = self.env.registry
        job_id = cjob.id

        def _forget():
            """Delete the mark, if there is one."""
            with registry.cursor() as cr:
                cr.execute(
                    "DELETE FROM cloud_job_no_return WHERE job_id = %s",
                    (job_id,),
                )

        run_after_commit(
            self.env.cr, f"the point-of-no-return mark of cloud.job {job_id}",
            _forget,
        )

    def _announce_done_after_commit(self, cjob, blocked):
        """Tell the watchers and the subscribers once *cjob*'s ``done`` commits.

        The bus event, and for a job that did not end blocked the mail,
        Telegram and webhook of a success (``_notify_by_email``,
        ``_notify_external``). Sent from the runner's transaction, they
        went out before its commit: a store of ``done`` that then failed
        — a lost serialization race, and queue_job runs the job again —
        announced a success that was not recorded, and the next attempt
        announced it a second time. After the commit, on a cursor of its
        own, never raising (``run_after_commit``): a worker that dies in
        between announces nothing, which the log does not show either.

        :param cjob: the ``cloud.job`` stored ``done``
        :param bool blocked: it ended blocked by an alert, and did
            nothing: the browser is told ``blocked``, which it does not
            toast ("Deploy Instance completed" was shown for a deploy
            that waited), and nobody is mailed
        """
        env = self.env
        job_id = cjob.id

        def _announce():
            """Broadcast and notify on a cursor of its own."""
            with env.registry.cursor() as cr:
                Job = env(cr=cr)["cloud.job"]
                Job._broadcast_job_update(
                    job_id, state="blocked" if blocked else "done",
                )
                job = Job.browse(job_id).exists()
                if blocked or not job:
                    return
                Job._notify_by_email(job, "done")
                Job._notify_external(job, "done")

        run_after_commit(
            self.env.cr, f"the announcement of cloud.job {job_id} done",
            _announce,
        )

    def _dismiss_failures_after_commit(self, cjob):
        """Dismiss *cjob*'s earlier failures once the job's own commit is in.

        Done in the job's transaction, the dismissal read the alerts as
        they were when the job started: one an operator dismissed by hand
        while the job ran was written again, the transaction lost the
        serialization race, and queue_job ran the whole job a second time
        (production, 2026-11-02: two ``UPDATE cloud_alert`` failures as
        alerts were dismissed by hand). On a cursor of its own after the
        commit it sees the alerts as they are now, and a failure there
        is logged: the job is done either way.

        Armed before ``_on_state_changed`` arms its own, so a retry that
        succeeded has dismissed its alert before the target's queue is
        looked at.

        :param cjob: the ``cloud.job`` that just reached ``done``.
        """
        env = self.env
        cjob_id = cjob.id

        def _dismiss():
            """Dismiss on a cursor of its own; never raises."""
            try:
                with env.registry.cursor() as cr:
                    job = env(cr=cr)['cloud.job'].browse(cjob_id)
                    if job.exists():
                        job.env['cloud.job']._dismiss_job_failed_alerts(job)
            except Exception:
                _logger.exception(
                    "could not dismiss the earlier failures of job %s", cjob_id,
                )

        self.env.cr.postcommit.add(_dismiss)

    def _seal_autopurge_clock(self, cjob):
        """Count a finished job as somebody having used the instance.

        This is the panel half of the staging activity clock, and it is
        deliberately narrow. Two kinds of job finish successfully on
        instances nobody is looking at: the background probes, which run
        every few minutes forever, and everything the platform bot
        starts on its own — a rebuild off a GitHub push above all. Either
        one counting would keep every staging alive indefinitely and turn
        the autopurge into dead code that looks alive.

        So the job has to be one a person could have started (not a
        hidden type) *and* actually authored by a person (an internal
        user that is not the bot). ``create_uid`` is trustworthy here
        because everything the platform starts on its own goes through
        ``as_platform``, which stamps the bot rather than whoever's
        request happened to trigger it.

        :param cjob: the ``cloud.job`` that just reached ``done``.
        """
        instance = cjob.instance_id
        if not instance:
            return
        if cjob.job_type_id.code in self.env['cloud.job']._get_hidden_job_types():
            return
        author = cjob.create_uid
        bot = self.env['res.users']._get_cron_bot()
        if not author or author.share or (bot and author.id == bot.id):
            return
        # The seal waits for the job's commit. The executor's hooks write
        # this same instance row on cursors of their own, and they commit
        # after this transaction took its snapshot: an UPDATE here loses
        # the serialization race, and queue_job then runs the whole job
        # again. Production, 2026-10-03: the first staging deploy since
        # the clock shipped ran twice and failed on the second pass.
        env = self.env
        instance_id = instance.id

        def _seal():
            """Seal the clock on a cursor of its own; never raises.

            By the time it runs the job is already done, so a failure
            here is logged rather than turned into a failed job.
            """
            try:
                with env.registry.cursor() as cr:
                    rec = env(cr=cr)['cloud.instance'].browse(instance_id)
                    # A finished delete unlinks the record after the
                    # same commit.
                    if rec.exists():
                        rec._touch_autopurge_clock()
            except Exception:
                _logger.exception(
                    "could not seal the autopurge clock of instance %s",
                    instance_id,
                )

        self.env.cr.postcommit.add(_seal)
