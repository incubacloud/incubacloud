"""A job whose worker died is closed, and what it left is put back.

queue_job finds a dead worker by its released lock and either puts the
job back in the queue or, once its retries are spent, fails it
``JobFoundDead`` — with SQL, so the cloud job never heard of it: it
stayed ``started`` for good, its queue busy, its move never rolled back
(6-nov-2026). ``cloud.job._cron_reconcile_lost_jobs`` closes it through
the ORM after its executor's hook, at most once. A job a person had
cancelled that queue_job puts back in the queue is not run again.

The death is simulated with the jobrunner's own ``UPDATE``.
"""
import threading
import time
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.abstract_executor import (
    CANCELLED_WHILE_RUNNING,
    AbstractExecutor,
    JobCancelled,
    JobWorkerLost,
)
from odoo.addons.incubacloud.models.cloud_job import _dispatch_scope_fresh
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.host_probe_executor import HostProbeExecutor
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from .test_blocked_and_aborted_jobs import _DeployJobCase

#: The jobrunner's ``requeue_dead_jobs``, for a job past its retries.
_FOUND_DEAD = (
    "UPDATE queue_job SET state = 'failed', exc_name = 'JobFoundDead',"
    " exc_info = 'Job found dead after too many retries' WHERE id = %s"
)
#: The same, for a job with retries left.
_REQUEUED = (
    "UPDATE queue_job SET state = 'pending', retry = COALESCE(retry, 0) + 1"
    " WHERE id = %s"
)


class _LostJobCase(_DeployJobCase):
    """A deploy job of an instance in 'deploying', and how its worker dies."""

    def setUp(self):
        super().setUp()
        self.Job = self.env["cloud.job"]
        self.qjob = self.job.queue_job_id
        self.job.with_context(ic_skip_state_hook=True).write({"state": "started"})
        self.env.flush_all()
        for name in ("_refresh_host_disk", "_publish_bus"):
            patcher = patch.object(AbstractExecutor, name, autospec=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _age(self, *jobs):
        """Put *jobs* past the reconciliation's margin, as a while ago."""
        self.env.flush_all()
        ids = tuple(job.id for job in jobs)
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date - interval '10 minutes'"
            " WHERE id IN %s", (ids,),
        )
        self.env.cr.execute(
            "UPDATE queue_job q"
            " SET date_created = q.date_created - interval '10 minutes',"
            " date_enqueued = (now() at time zone 'UTC') - interval '10 minutes'"
            " FROM cloud_job c WHERE c.queue_job_uuid = q.uuid AND c.id IN %s",
            (ids,),
        )
        self.env.invalidate_all()

    def _die(self, qjob=None, sql=_FOUND_DEAD):
        """The worker of *qjob* died, and queue_job found it so."""
        qjob = qjob or self.qjob
        self.env.flush_all()
        self.env.cr.execute(sql, (qjob.id,))
        self.env.invalidate_all()

    def _reconcile(self):
        """One pass of the cron, with what it read invalidated after."""
        closed = self.Job._cron_reconcile_lost_jobs()
        self.env.invalidate_all()
        return closed

    def _log(self, job=None):
        """The job's log, as one text."""
        return "\n".join(
            self.env["cloud.job.log.chunk"].search(
                [("job_id", "=", (job or self.job).id)], order="id",
            ).mapped("content")
        )

    def _failures(self, job=None):
        """The job's ``job_failed`` alerts."""
        return self.env["cloud.alert"].search([
            ("job_id", "=", (job or self.job).id), ("code", "=", "job_failed"),
        ])

    def _chained(self):
        """A step waiting on ``self.job`` in its graph.

        :return: the step's ``cloud.job``
        """
        tail = self._deploy_job("uuid-tail")
        tail.with_context(ic_skip_state_hook=True).write(
            {"state": "wait_dependencies"},
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE queue_job SET graph_uuid = 'graph-lost',"
            " state = CASE WHEN id = %s THEN 'wait_dependencies' ELSE state END"
            " WHERE id IN %s",
            (tail.queue_job_id.id, (self.qjob.id, tail.queue_job_id.id)),
        )
        self.env.invalidate_all()
        return tail


class TestALostWorkerIsReconciled(_LostJobCase):
    """Case A: the job is closed failed after its ``on_abort``."""

    def test_a_job_found_dead_is_closed_failed_once(self):
        self._age(self.job)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort, patch.object(
            type(self.Job), "_schedule_dispatch", autospec=True,
        ) as dispatch:
            self.assertEqual(self._reconcile(), 1)
        self.assertEqual(self.job.state, "failed")
        self.assertEqual(self.qjob.state, "failed")
        self.assertTrue(self.job.worker_lost_at)
        on_abort.assert_called_once()
        self.assertIsInstance(on_abort.call_args.args[1], JobWorkerLost)
        self.assertEqual(len(self._failures()), 1)
        self.assertIn("queue_job gave up on it", self._log())
        self.assertTrue(any(
            (self.inst.id, None) in call.args[1] for call in dispatch.call_args_list
        ), "the instance's queue is served once the job is closed")

    def test_its_end_is_written_in_the_language_of_the_pass(self):
        """What the job ended on, in its queue.job and its alert, is
        translated as its log line is."""
        self.env["res.lang"]._activate_lang("es_ES")
        self._age(self.job)
        self._die()
        with patch.object(DeployInstanceExecutor, "on_abort", autospec=True):
            self.assertEqual(
                self.Job.with_context(lang="es_ES")._cron_reconcile_lost_jobs(),
                1,
            )
        self.env.invalidate_all()
        spanish = "El proceso que ejecutaba este trabajo murió y queue_job"
        self.assertTrue(self.qjob.exc_message.startswith(spanish))
        self.assertIn(spanish, self._failures().message)

    def test_its_hook_puts_the_instance_back(self):
        """Unpatched: the deploy's own ``on_abort`` takes it out of
        'deploying', as for any failure before its steps."""
        self._age(self.job)
        self._die()
        self._reconcile()
        self.assertEqual(self.inst.state, "draft")
        self.assertEqual(self.inst.status, "error")
        self.assertIn("JobWorkerLost", self._log())

    def test_its_queue_is_free_after_it(self):
        self._age(self.job)
        self._die()
        self.assertTrue(self.Job._scope_busy(self.inst.id, self.host.id))
        self._reconcile()
        self.assertFalse(self.Job._scope_busy(self.inst.id, self.host.id))

    def test_a_hook_that_raises_still_closes_it(self):
        self._age(self.job)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
            side_effect=RuntimeError("hook broke"),
        ) as on_abort:
            self.assertEqual(self._reconcile(), 1)
        on_abort.assert_called_once()
        self.assertEqual(self.job.state, "failed")
        self.assertEqual(len(self._failures()), 1)

    def test_a_second_pass_does_nothing_more(self):
        self._age(self.job)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort:
            self._reconcile()
            self.assertEqual(self._reconcile(), 0)
        on_abort.assert_called_once()
        self.assertEqual(len(self._failures()), 1)

    def test_a_pass_cut_off_before_the_close_closes_it_next_without_its_hook(self):
        self._age(self.job)
        self._die()
        real_close = type(self.Job)._close_lost_job
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort, patch.object(
            type(self.Job), "_close_lost_job", autospec=True,
            side_effect=RuntimeError("the cron died"),
        ):
            self.assertEqual(self._reconcile(), 0)
        self.assertEqual(self.job.state, "started")
        self.assertTrue(self.job.worker_lost_at)
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as again, patch.object(
            type(self.Job), "_close_lost_job", autospec=True,
            side_effect=real_close,
        ):
            self.assertEqual(self._reconcile(), 1)
        on_abort.assert_called_once()
        again.assert_not_called()
        self.assertEqual(self.job.state, "failed")
        self.assertIn("An earlier pass took this job up", self._log())

    def test_the_steps_after_it_are_cancelled_and_let_go(self):
        tail = self._chained()
        self._age(self.job, tail)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ), patch.object(
            DeployInstanceExecutor, "on_never_run", autospec=True,
        ) as on_never_run:
            self._reconcile()
        self.assertEqual(self.job.state, "failed")
        self.assertEqual(tail.state, "cancelled")
        self.assertEqual(tail.queue_job_id.state, "cancelled")
        on_never_run.assert_called_once()
        self.assertIn("ended failed", self._log(tail))
        self.assertFalse(self._failures(tail))

    def test_a_job_whose_queue_job_is_gone_is_closed_failed(self):
        self._age(self.job)
        self.env.cr.execute("DELETE FROM queue_job WHERE id = %s", (self.qjob.id,))
        self.env.invalidate_all()
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort:
            self.assertEqual(self._reconcile(), 1)
        on_abort.assert_called_once()
        self.assertEqual(self.job.state, "failed")
        self.assertEqual(len(self._failures()), 1)
        self.assertFalse(self.Job._scope_busy(self.inst.id, self.host.id))
        self.assertIn("record is gone", self._log())


class TestWhatIsNotLost(_LostJobCase):
    """The jobs the reconciliation leaves alone."""

    def _untouched(self):
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort, patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            self.assertEqual(self._reconcile(), 0)
        on_abort.assert_not_called()
        on_cancelled.assert_not_called()
        self.assertFalse(self.job.worker_lost_at)

    def test_a_slow_job_is_queue_job_s(self):
        self._age(self.job)
        self._untouched()
        self.assertEqual(self.job.state, "started")

    def test_a_job_put_back_in_the_queue_is_queue_job_s(self):
        self._age(self.job)
        self._die(sql=_REQUEUED)
        self._untouched()
        self.assertEqual(self.job.state, "started")

    def test_a_death_within_the_margin_waits(self):
        self._die()
        self._untouched()
        self.assertEqual(self.job.state, "started")

    def test_a_cancelled_job_whose_queue_job_is_gone_is_history(self):
        self.job.with_context(ic_skip_state_hook=True).write({"state": "cancelled"})
        self._age(self.job)
        self.env.cr.execute("DELETE FROM queue_job WHERE id = %s", (self.qjob.id,))
        self.env.invalidate_all()
        self._untouched()
        self.assertEqual(self.job.state, "cancelled")


class TestTheOtherEnds(_LostJobCase):
    """Cases B, C, C' and D."""

    def test_a_stop_whose_worker_died_ends_cancelled_after_its_hook(self):
        self.job.cancel_job()
        self._age(self.job)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            self.assertEqual(self._reconcile(), 1)
        on_cancelled.assert_called_once()
        _executor, results, exc = on_cancelled.call_args.args
        self.assertIsNone(results)
        self.assertIsInstance(exc, JobCancelled)
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertFalse(self._failures())
        self.assertFalse(self.Job._scope_busy(self.inst.id, self.host.id))

    def test_a_success_before_the_stop_ends_done_without_a_hook(self):
        self.job.cancel_job()
        self.env.flush_all()
        DeployInstanceExecutor(self.job, self.host)._mark_success_before_cancel()
        self._age(self.job)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            self._reconcile()
        on_cancelled.assert_not_called()
        self.assertEqual(self.qjob.state, "done")
        self.assertEqual(self.job.state, "done")

    def _mark_the_outcome(self):
        """What a run whose ``on_success`` committed leaves: its outcome mark."""
        self.env["cloud.job.outcome"].sudo().create({
            "job_id": self.job.id,
            "queue_job_uuid": self.qjob.uuid,
            "own_job_vals": {"result": {"saved": "outcome"}},
            "owed": [],
        })
        self.env.flush_all()

    def test_a_success_saved_before_the_death_ends_done_without_a_hook(self):
        """Found dead after its ``on_success`` committed: done, its own row back."""
        self._mark_the_outcome()
        self._age(self.job)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort:
            self.assertEqual(self._reconcile(), 1)
        on_abort.assert_not_called()
        self.assertEqual(self.qjob.state, "done")
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.job.result, {"saved": "outcome"})
        self.assertFalse(self._failures())
        self.assertIn("its outcome was saved", self._log())

    def test_a_stop_after_a_saved_success_ends_done_without_a_hook(self):
        """Stopped after the mark, worker found dead: no stop hook, done."""
        self._mark_the_outcome()
        self.job.cancel_job()
        self._age(self.job)
        self._die()
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            self.assertEqual(self._reconcile(), 1)
        on_cancelled.assert_not_called()
        self.assertEqual(self.qjob.state, "done")
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.job.result, {"saved": "outcome"})

    def test_a_saved_success_whose_queue_job_is_gone_is_closed_done(self):
        """Its queue.job gone, the mark there: done, no hook, no failure alert."""
        self._mark_the_outcome()
        self._age(self.job)
        self.env.cr.execute("DELETE FROM queue_job WHERE id = %s", (self.qjob.id,))
        self.env.invalidate_all()
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort:
            self.assertEqual(self._reconcile(), 1)
        on_abort.assert_not_called()
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.job.result, {"saved": "outcome"})
        self.assertFalse(self._failures())

    def test_a_queue_job_cancelled_before_it_started_lets_go(self):
        self.job.with_context(ic_skip_state_hook=True).write({"state": "pending"})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'cancelled',"
            " date_cancelled = (now() at time zone 'UTC') - interval '10 minutes'"
            " WHERE id = %s", (self.qjob.id,),
        )
        self._age(self.job)
        with patch.object(
            DeployInstanceExecutor, "on_never_run", autospec=True,
        ) as on_never_run, patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            self.assertEqual(self._reconcile(), 1)
        on_never_run.assert_called_once()
        on_cancelled.assert_not_called()
        self.assertEqual(self.job.state, "cancelled")

    def test_a_queue_job_cancelled_while_it_ran_is_closed_as_a_stop(self):
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'cancelled' WHERE id = %s", (self.qjob.id,),
        )
        self._age(self.job)
        with patch.object(
            DeployInstanceExecutor, "on_never_run", autospec=True,
        ) as on_never_run, patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            self.assertEqual(self._reconcile(), 1)
        on_cancelled.assert_called_once()
        on_never_run.assert_not_called()
        self.assertEqual(self.job.state, "cancelled")

    def test_a_queue_job_done_is_closed_done_without_a_hook(self):
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'done',"
            " date_done = (now() at time zone 'UTC') - interval '10 minutes'"
            " WHERE id = %s", (self.qjob.id,),
        )
        self._age(self.job)
        with patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
        ) as on_abort:
            self.assertEqual(self._reconcile(), 1)
        on_abort.assert_not_called()
        self.assertEqual(self.job.state, "done")
        self.assertFalse(self._failures())


class TestAStopIsNotRunAgain(_LostJobCase):
    """Section A: a cancelled job queue_job puts back in the queue."""

    def _requeued_after_a_stop(self):
        """Cancelled while it ran; its worker died and it starts again."""
        self.job.cancel_job()
        self._die(sql=_REQUEUED)
        # What ``_acquire_job`` stores once the jobrunner hands it out.
        self.qjob.write({"state": "started"})
        self.env.invalidate_all()

    def test_it_starts_again_still_cancelled(self):
        self._requeued_after_a_stop()
        self.assertEqual(self.qjob.state, "started")
        self.assertEqual(self.job.state, "cancelled")

    def test_it_does_not_run_and_is_closed_as_a_stop(self):
        tail = self._chained()
        self._requeued_after_a_stop()
        with patch.object(
            DeployInstanceExecutor, "run", autospec=True,
        ) as run, patch.object(
            DeployInstanceExecutor, "pre_run_checks", autospec=True,
        ) as checks, patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled, patch.object(
            DeployInstanceExecutor, "_flush_logs", autospec=True,
        ) as flush:
            result = self.job.execute()
        self.assertEqual(result, CANCELLED_WHILE_RUNNING)
        run.assert_not_called()
        checks.assert_not_called()
        on_cancelled.assert_called_once()
        self.assertIsNone(on_cancelled.call_args.args[1])
        flush.assert_called()
        AbstractExecutor._publish_bus.assert_called()
        # What queue_job stores once ``execute`` returned the stop.
        self.qjob.write({"state": "done", "result": result})
        self.env.invalidate_all()
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(tail.state, "cancelled")
        self.assertFalse(self._failures())

    def test_its_hook_puts_the_instance_back(self):
        self._requeued_after_a_stop()
        with patch.object(DeployInstanceExecutor, "run", autospec=True) as run:
            self.job.execute()
        run.assert_not_called()
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.state, "draft")
        self.assertIn("it is not run.", self._log())

    def test_a_success_before_the_stop_is_done_and_its_chain_goes_on(self):
        tail = self._chained()
        self.job.cancel_job()
        self.env.flush_all()
        DeployInstanceExecutor(self.job, self.host)._mark_success_before_cancel()
        self._die(sql=_REQUEUED)
        self.qjob.write({"state": "started"})
        self.env.invalidate_all()
        self.assertTrue(self.job.succeeded_before_cancel)
        with patch.object(
            DeployInstanceExecutor, "run", autospec=True,
        ) as run, patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            result = self.job.execute()
        run.assert_not_called()
        on_cancelled.assert_not_called()
        self.qjob.write({"state": "done", "result": result})
        self.env.invalidate_all()
        self.assertEqual(self.qjob.state, "done")
        self.assertEqual(self.job.state, "done")
        self.assertEqual(tail.state, "wait_dependencies")

    def test_a_start_nobody_cancelled_runs_and_clears_the_claim(self):
        self.env.cr.execute(
            "UPDATE cloud_job SET worker_lost_at = now() WHERE id = %s",
            (self.job.id,),
        )
        self._die(sql=_REQUEUED)
        self.qjob.write({"state": "started"})
        self.env.invalidate_all()
        self.assertEqual(self.job.state, "started")
        self.assertFalse(self.job.worker_lost_at)

    def test_a_new_attempt_clears_the_sweep_s_sight_of_a_dead_stop(self):
        """B2: requeued by hand and dead again, it is a new death: its
        alert counts from the sweep's new sight, not the old one."""
        self.env.cr.execute(
            "UPDATE cloud_job SET found_dead_seen_at = now() WHERE id = %s",
            (self.job.id,),
        )
        self._die(sql=_REQUEUED)
        self.qjob.write({"state": "started"})
        self.env.invalidate_all()
        self.assertEqual(self.job.state, "started")
        self.assertFalse(self.job.found_dead_seen_at)


class _DeadStopCase(_LostJobCase):
    """A job cancelled while it ran, whose worker queue_job then found dead."""

    def setUp(self):
        super().setUp()
        self.job.cancel_job()
        self._die()

    def _queued(self, code="restart_instance"):
        """A job of *code* waiting in the instance's queue.

        :return: the ``cloud.job``
        """
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        self.assertTrue(job_type, code)
        job = self.Job.sudo().create({
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": job_type.id,
            "name": code,
        })
        job.with_context(ic_skip_state_hook=True).write({"state": "queued"})
        self.env.flush_all()
        return job

    def _dispatches(self):
        """Gather the queues the transactions under test ask to serve.

        What their commit would serve (``_schedule_dispatch``): a test's
        cursors never run what waits for a commit.

        :return: the patcher's context, and the list the scopes go to
        """
        scopes = []
        patcher = patch.object(
            type(self.Job), "_schedule_dispatch", autospec=True,
            side_effect=lambda _model, served: scopes.extend(served),
        )
        return patcher, scopes

    def _serve(self, scopes):
        """Serve *scopes* as the commit that asked for them would."""
        for scope in dict.fromkeys(scopes):
            _dispatch_scope_fresh(self.env.registry, scope)
        self.env.invalidate_all()

    def _stopping(self, job=None):
        """Whether *job* (this one) reads *Cancelling…*."""
        job = job or self.job
        return job.id in self.Job._stopping_ids([("id", "=", job.id)])


class TestADeadStopHoldsItsQueue(_DeadStopCase):
    """#96: the queue waits for the reconciliation's close, not its claim.

    The reconciliation runs the job's hook (``on_cancelled_running``)
    between its claim and its close, with no lock held: a job started
    beside it would run with what the stop is putting back on the server.
    """

    def test_it_holds_its_queue_and_the_sweep_starts_nothing(self):
        waiting = self._queued()
        self.assertTrue(self.Job._scope_busy(self.inst.id, self.host.id))
        self.Job._cron_dispatch_queued()
        self.env.invalidate_all()
        self.assertEqual(waiting.state, "queued")

    def test_claimed_and_cleaned_up_it_holds_until_its_close(self):
        """Not let go by ``worker_lost_at``: the hook runs after the claim."""
        self._age(self.job)
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled:
            self.assertEqual(
                self.Job._claim_lost_job(self.job.id), ("stopped", False),
            )
            self.Job._clean_up_lost_job(self.job.id, "stopped", False)
        on_cancelled.assert_called_once()
        self.env.invalidate_all()
        self.assertTrue(self.job.worker_lost_at)
        self.assertEqual(self.job.state, "cancelled")
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))
        self.assertTrue(self._stopping())

    def test_its_close_frees_the_queue_and_serves_it_at_once(self):
        """In the close's own transaction, without waiting for the sweep."""
        waiting = self._queued()
        self._age(self.job)
        patcher, scopes = self._dispatches()
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ), patcher:
            self.assertEqual(self._reconcile(), 1)
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertFalse(self.qjob.exc_name)
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))
        self.assertFalse(self._stopping())
        self.assertIn((self.inst.id, None), scopes)
        self._serve(scopes)
        self.assertEqual(waiting.state, "pending")

    def test_a_step_of_a_chain_holds_the_chain_s_other_queues(self):
        """A staging's restore cancelled and dead: production, where the
        chain's download ran, waits too."""
        other = self.env["cloud.instance"].create({
            "name": "lost-staging",
            "project_id": self.inst.project_id.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        download = self.Job.sudo().create({
            "host_id": self.host.id,
            "instance_id": other.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "backup_download")], limit=1,
            ).id,
            "name": "Download",
            "queue_chain_key": "chain-lost",
        })
        download.with_context(ic_skip_state_hook=True).write({"state": "done"})
        self.job.sudo().write({"queue_chain_key": "chain-lost"})
        self.env.flush_all()
        self.assertTrue(self.Job._scope_busy(other.id, None))

    def test_it_reads_cancelling_without_a_step_apart(self):
        """No checkpoint: the queue.job alone says it still stops."""
        self.assertFalse(self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", self.job.id)],
        ))
        self.assertTrue(self._stopping())

    def test_a_hidden_job_holds_nothing_unless_it_is_a_step_of_a_chain(self):
        """A chain is one entry, its hidden steps included: a move's
        fingerprint holds the move's instance as its visible steps do."""
        hidden = self.env["cloud.job.type"].search(
            [("code", "=", "move_fingerprint")], limit=1,
        )
        self.assertIn(hidden.code, self.Job._get_hidden_job_types())
        self.job.sudo().write({"job_type_id": hidden.id})
        self.env.flush_all()
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))
        self.job.sudo().write({"queue_chain_key": "chain-move"})
        self.env.flush_all()
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))

    def test_a_stop_whose_queue_job_ended_holds_nothing(self):
        for state in ("cancelled", "done"):
            with self.subTest(state=state):
                self.env.flush_all()
                self.env.cr.execute(
                    "UPDATE queue_job SET state = %s, exc_name = NULL"
                    " WHERE id = %s", (state, self.qjob.id),
                )
                self.env.invalidate_all()
                self.assertFalse(self.Job._scope_busy(self.inst.id, None))
                self.assertFalse(self._stopping())

    def test_a_hook_that_queues_in_its_own_queue_waits_for_the_close(self):
        """M2: what the hook queues on its instance (a move's rollback)
        waits while the hook runs, and starts once the job is closed."""
        self._age(self.job)
        seen = {}

        async def hook(executor, results, exc):
            """Read the queue and queue in it, as a rollback would."""
            Job = executor.env["cloud.job"].sudo()
            seen["busy"] = Job._scope_busy(self.inst.id, None)
            seen["job"] = Job.enqueue(
                self.host.id, self.inst.id, "restart_instance",
                bypass_running_check=True,
            )
            seen["state"] = Job.browse(seen["job"]).state

        patcher, scopes = self._dispatches()
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
            side_effect=hook,
        ), patcher:
            self.assertEqual(self._reconcile(), 1)
        self.assertTrue(seen["busy"])
        self.assertEqual(seen["state"], "queued")
        self.assertEqual(self.job.state, "cancelled")
        self._serve(scopes)
        self.assertEqual(self.Job.browse(seen["job"]).state, "pending")


class TestADeadStopNobodyClosesIsSaid(_DeadStopCase):
    """M1: the queue it holds is said, and a person can let it go."""

    def _alert(self):
        """The job's alert of a stop nobody closes."""
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", "job_stop_unclosed"), ("job_id", "=", self.job.id),
        ])

    def _seen_ago(self, minutes):
        """Put the sweep's first sight of the dead stop *minutes* back."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_job SET found_dead_seen_at ="
            " (now() at time zone 'UTC') - make_interval(mins => %s)"
            " WHERE id = %s", (minutes, self.job.id),
        )
        self.env.invalidate_all()

    def _alerted(self):
        """The sweep has seen it, and alerts once it has waited long enough.

        :return: the alert
        """
        self.Job._cron_dispatch_queued()
        self._seen_ago(31)
        self.Job._cron_dispatch_queued()
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        return alert

    def _write_date(self):
        """The job's ``write_date``, as the database has it."""
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT write_date FROM cloud_job WHERE id = %s", (self.job.id,),
        )
        return self.env.cr.fetchone()[0]

    def test_the_sweep_alerts_once_nobody_closes_it(self):
        """Counted from the sweep's first sight of it, the reconciliation
        never run; the alert asks, and the queue still waits. The sight
        leaves ``write_date`` alone: the reconciliation's margin counts
        from it."""
        written = self._write_date()
        self.Job._cron_dispatch_queued()
        self.env.invalidate_all()
        self.assertTrue(self.job.found_dead_seen_at)
        self.assertEqual(self._write_date(), written)
        self.assertFalse(self._alert())
        self._seen_ago(29)
        self.Job._cron_dispatch_queued()
        self.assertFalse(self._alert())
        self._seen_ago(31)
        self.Job._cron_dispatch_queued()
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.state, "active")
        self.assertEqual(alert.instance_id, self.inst)
        self.assertEqual(alert.host_id, self.host)
        self.assertIn(f"#{self.job.id}", alert.message)
        self.assertIn(self.inst.name, alert.message)
        self.Job._cron_dispatch_queued()
        self.assertEqual(len(self._alert()), 1)
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))
        self.assertFalse(self.job.worker_lost_at)

    def test_a_stop_claimed_and_never_closed_is_said_too(self):
        self._age(self.job)
        self.assertEqual(self.Job._claim_lost_job(self.job.id), ("stopped", False))
        self._alerted()
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))

    def test_a_hidden_job_that_holds_nothing_is_not_said(self):
        hidden = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )
        self.job.sudo().write({"job_type_id": hidden.id})
        self.Job._cron_dispatch_queued()
        self._seen_ago(31)
        self.Job._cron_dispatch_queued()
        self.assertFalse(self._alert())

    def test_it_closes_by_itself_once_the_reconciliation_closes_the_job(self):
        alert = self._alerted()
        self._age(self.job)
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ):
            self.assertEqual(self._reconcile(), 1)
        self.Job._cron_dispatch_queued()
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(alert.resolution, "auto")

    def _checkpoint(self):
        """The job's checkpoint, as a step apart would have left it."""
        return self.env["cloud.job.checkpoint"].sudo().create({
            "job_id": self.job.id, "host_id": self.host.id, "parked": True,
        })

    def _dismiss(self, alert):
        """Dismiss *alert* by hand, its job's hook watched.

        :return: the hook's mock, and the scopes the close asked to serve
        """
        patcher, scopes = self._dispatches()
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as on_cancelled, patcher:
            alert.write({"state": "dismissed", "resolution": "manual"})
        self.env.invalidate_all()
        return on_cancelled, scopes

    def test_dismissing_it_closes_the_job_without_its_hook(self):
        """What the lost run left is not put back by a hook: the
        checkpoint is closed, not deleted, the log says so and names
        the instance's state, and the queue is served."""
        row = self._checkpoint()
        waiting = self._queued()
        alert = self._alerted()
        on_cancelled, scopes = self._dismiss(alert)
        on_cancelled.assert_not_called()
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertTrue(row.exists())
        self.assertTrue(row.closed)
        self.assertFalse(row.parked)
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))
        self.assertFalse(self._stopping())
        log = self._log()
        self.assertIn("A person closed the alert", log)
        self.assertIn(f"Instance {self.inst.name} is left Deploying", log)
        self._serve(scopes)
        self.assertEqual(waiting.state, "pending")

    def test_dismissed_its_step_apart_is_cleaned_up_by_the_next_pass(self):
        """M1: the closed checkpoint is only a server to clean
        (``checkpoint``): the next pass stops and removes what it names,
        with no ``on_cancelled_running``, and deletes it."""
        self._checkpoint()
        self._dismiss(self._alerted())
        self._age(self.job)
        self.assertEqual(
            self.Job._claim_lost_job(self.job.id), ("checkpoint", False),
        )
        cases = []
        clean_up = type(self.Job)._clean_up_lost_job

        def spy(model, job_id, case, resumed):
            """Note the case the pass takes the job up as."""
            cases.append(case)
            return clean_up(model, job_id, case, resumed)

        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as again, patch.object(
            AbstractExecutor, "_close_on_server", autospec=True,
        ) as on_server, patch.object(
            type(self.Job), "_clean_up_lost_job", autospec=True,
            side_effect=spy,
        ):
            self.assertEqual(self._reconcile(), 0)
        again.assert_not_called()
        on_server.assert_called_once()
        self.assertEqual(cases, ["checkpoint"])
        self.assertIn("something left on its server", self._log())
        self.assertFalse(self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", self.job.id)],
        ))
        self.assertEqual(self.job.state, "cancelled")

    def test_without_a_checkpoint_nothing_is_left_for_the_reconciliation(self):
        self._dismiss(self._alerted())
        self._age(self.job)
        with patch.object(
            DeployInstanceExecutor, "on_cancelled_running", autospec=True,
        ) as again:
            self.assertEqual(self._reconcile(), 0)
        again.assert_not_called()

    def test_dismissed_while_the_reconciliation_runs_its_hook_it_says_so(self):
        """B1: claimed, and the reconciliation runs now: the close goes on
        (a hook that hangs is a reason for the alert), said in the job's
        log and the server's."""
        self._age(self.job)
        self.assertEqual(
            self.Job._claim_lost_job(self.job.id), ("stopped", False),
        )
        alert = self._alerted()
        with patch.object(
            type(self.Job), "_cron_runs_now", autospec=True, return_value=True,
        ), self.assertLogs(
            "odoo.addons.incubacloud.models.cloud_job", "WARNING",
        ) as logs:
            on_cancelled, _scopes = self._dismiss(alert)
        on_cancelled.assert_not_called()
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertIn("reconciliation is running", self._log())
        self.assertTrue(any(
            "reconciliation is running" in line for line in logs.output
        ))

    def test_dismissed_with_the_reconciliation_idle_it_says_nothing_of_it(self):
        self._age(self.job)
        self.Job._claim_lost_job(self.job.id)
        alert = self._alerted()
        with patch.object(
            type(self.Job), "_cron_runs_now", autospec=True, return_value=False,
        ):
            self._dismiss(alert)
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertNotIn("reconciliation is running", self._log())

    def test_dismissed_by_the_platform_it_closes_nothing(self):
        alert = self._alerted()
        alert.write({"state": "dismissed"})
        self.env.invalidate_all()
        self.assertEqual(self.qjob.state, "failed")
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))

    def test_dismiss_all_leaves_it_open(self):
        alert = self._alerted()
        self.env["cloud.alert"].action_dismiss_all()
        self.assertEqual(alert.state, "active")


class TestAStopBetweenTwoTicks(TransactionCase):
    """The postpone of a job that runs in ticks, stored as a stop."""

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        host = self.env["cloud.host"].create({
            "name": "ticks-host", "ip_address": "10.0.0.81", "user": "ubuntu",
            "wildcard_domain": "ticks.example.com",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1,
        )
        self.qjob = self.env["queue.job"].sudo().create({
            "uuid": "uuid-ticks", "name": "ticks", "state": "started",
            "method_name": "noop", "model_name": "cloud.job", "func_string": "noop()",
        })
        self.cjob = self.env["cloud.job"].sudo().create({
            "host_id": host.id, "job_type_id": job_type.id, "name": "Ticks",
            "queue_job_uuid": self.qjob.uuid, "queue_job_id": self.qjob.id,
        })

    def _postponed_after_the_stop(self):
        """The stop is committed; then queue_job stores the tick's postpone."""
        self.cjob.cancel_job()
        self.env.flush_all()
        self.qjob.write({"state": "pending", "eta": "2026-11-06 08:12:30"})
        self.env.invalidate_all()

    def test_a_job_that_runs_in_ticks_lets_go(self):
        with patch.object(
            HostProbeExecutor, "_postpones_between_ticks", True,
        ), patch.object(
            HostProbeExecutor, "on_never_run", autospec=True,
        ) as on_never_run:
            self._postponed_after_the_stop()
        on_never_run.assert_called_once()
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertEqual(self.cjob.state, "cancelled")

    def test_another_job_is_left_as_it_is(self):
        with patch.object(
            HostProbeExecutor, "on_never_run", autospec=True,
        ) as on_never_run:
            self._postponed_after_the_stop()
        on_never_run.assert_not_called()
        self.assertEqual(self.qjob.state, "cancelled")


class TestATickPostponedAfterAStop(TestAStopBetweenTwoTicks):
    """4b of the VPS order, end to end: through ``execute`` and queue_job.

    The tick reads no flag, the stop is committed, the tick asks to come
    back later, and queue_job stores the postpone as its controller does
    (``retry_postpone``).
    """

    # The two tests of the case above, with this way of reaching it.
    allow_inherited_tests_method = True

    def _postponed_after_the_stop(self):
        """Run one tick through queue_job's ``Job``; the stop lands in it."""
        self.qjob.with_context(_job_edit_sentinel=self.qjob.EDIT_SENTINEL).write({
            "method_name": "execute", "records": self.cjob,
        })
        self.cjob.with_context(ic_skip_state_hook=True).write({"state": "started"})
        self.env.flush_all()
        cjob = self.cjob

        def tick(_executor):
            """Nothing read the flag; the stop commits; come back later."""
            cjob.cancel_job()
            cjob.env.flush_all()
            raise RetryableJobError("next tick", seconds=30)

        job = Job.load(self.env, self.qjob.uuid)
        with patch.object(
            HostProbeExecutor, "run", autospec=True, side_effect=tick,
        ), patch.object(HostProbeExecutor, "pre_run_checks", autospec=True):
            try:
                job.perform()
            except RetryableJobError as err:
                with job.in_temporary_env():
                    job.postpone(result=str(err), seconds=err.seconds)
                    job.set_pending(reset_retry=False)
                    job.store()
            else:
                self.fail("the tick did not ask to come back")
        self.env.invalidate_all()


class TestALockedJobIsSkipped(TransactionCase):
    """A row another transaction holds is left for the next pass, at once.

    Not in test mode: the rows are committed, and held by a real
    connection of their own.
    """

    def setUp(self):
        super().setUp()
        registry = self.env.registry
        with registry.cursor() as cr:
            env = self.env(cr=cr)
            # A server of the database's: creating one commits what its
            # removal would then have to undo, outside this test.
            host = env["cloud.host"].with_context(active_test=False).search(
                [], limit=1,
            )
            if not host:
                self.skipTest("no server in this database to hang a job on")
            job_type = env["cloud.job.type"].search(
                [("code", "=", "host_probe")], limit=1,
            )
            qjob = env["queue.job"].sudo().create({
                "uuid": "uuid-locked", "name": "locked", "state": "failed",
                "exc_name": "JobFoundDead",
                "method_name": "noop", "model_name": "cloud.job",
                "func_string": "noop()",
            })
            job = env["cloud.job"].sudo().create({
                "host_id": host.id, "job_type_id": job_type.id, "name": "Locked",
                "queue_job_uuid": qjob.uuid, "queue_job_id": qjob.id,
            })
            job.with_context(ic_skip_state_hook=True).write({"state": "started"})
            env.flush_all()
            cr.execute(
                "UPDATE cloud_job SET write_date = write_date - interval '10 minutes'"
                " WHERE id = %s", (job.id,),
            )
            cr.execute(
                "UPDATE queue_job SET date_created ="
                " (now() at time zone 'UTC') - interval '10 minutes'"
                " WHERE id = %s", (qjob.id,),
            )
            self.ids = (qjob.id, job.id)
        self.addCleanup(self._remove)

    def _remove(self):
        """Delete the committed rows."""
        qjob_id, job_id = self.ids
        with self.env.registry.cursor() as cr:
            cr.execute("DELETE FROM cloud_job_log_chunk WHERE job_id = %s", (job_id,))
            cr.execute("DELETE FROM cloud_alert WHERE job_id = %s", (job_id,))
            cr.execute("DELETE FROM cloud_job WHERE id = %s", (job_id,))
            cr.execute("DELETE FROM queue_job WHERE id = %s", (qjob_id,))

    def test_a_row_held_elsewhere_is_skipped_without_waiting(self):
        _qjob_id, job_id = self.ids
        Job = self.env["cloud.job"]
        with self.env.registry.cursor() as holder:
            holder.execute(
                "SELECT id FROM cloud_job WHERE id = %s FOR UPDATE", (job_id,),
            )
            outcome = {}

            def claim():
                began = time.monotonic()
                outcome["claim"] = Job._claim_lost_job(job_id)
                outcome["seconds"] = time.monotonic() - began

            # In a thread, so a wait would show as a timeout, not a hang.
            worker = threading.Thread(target=claim)
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "the claim waited for the lock")
            holder.rollback()
        self.assertIsNone(outcome["claim"])
        self.assertLess(outcome["seconds"], 5)
        self.assertEqual(Job._claim_lost_job(job_id), ("failed", False))
