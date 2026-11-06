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
