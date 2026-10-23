import logging

from odoo import _, fields, models

from .abstract_executor import BLOCKED_BY_ALERT, CANCELLED_WHILE_RUNNING

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
            stopped = candidates._cancelled_while_running()
            if stopped:
                (self - stopped).write(vals)
                return stopped.with_context(
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
        result = super().write(vals)
        new_state = vals.get("state")
        if new_state not in _MIRRORED:
            return result

        for qjob in self:
            cloud_jobs = self.env["cloud.job"].search(
                [("queue_job_uuid", "=", qjob.uuid)],
                limit=1,
            )
            if not cloud_jobs:
                continue

            cjob = cloud_jobs

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
            cjob.sudo().with_context(ic_skip_state_hook=True).write(
                {"state": new_state},
            )
            # To the row now: ``_check_cancel`` and the stop detection
            # above read it with SQL, from other transactions and from
            # this one.
            cjob.flush_recordset(["state"])
            _logger.info(
                "[queue_job_ext] uuid=%s state=%s -> cloud.job id=%s updated",
                qjob.uuid, new_state, cjob.id,
            )
            if new_state not in _TERMINAL:
                continue

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
            # created for done/cancelled.
            blocked = qjob.result == BLOCKED_BY_ALERT
            # queue_job stores a job that ended blocked as done; the
            # browser is told 'blocked', which it does not toast. "Deploy
            # Instance completed" was shown for a deploy that waited.
            self.env['cloud.job']._broadcast_job_update(
                cjob.id, state='blocked' if blocked else new_state,
            )
            # A job that ended blocked did nothing: there is no success to
            # announce, no failure to dismiss and nobody's activity to
            # count. The alert it waits for is what people are told about.
            if blocked:
                continue
            if new_state != 'failed':
                self.env['cloud.job']._notify_by_email(cjob, new_state)
                self.env['cloud.job']._notify_external(cjob, new_state)
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
                CJob._dismiss_job_failed_alerts(cjob)
                self._seal_autopurge_clock(cjob)
            # The end is acted on last, with its alerts recorded: a
            # failure that holds the queue has its alert, and a retry
            # that succeeded has dismissed it, before the next entry of
            # the target's queue is looked at — once this transaction
            # commits, on a cursor of its own. Nothing raised there, or
            # by a layer's hook, reaches the runner's ``store()``.
            cjob._on_state_changed(new_state)

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
        which ``rollback_move`` reads).

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
        Job._schedule_dispatch(Job._queue_scopes_of(steps))

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
