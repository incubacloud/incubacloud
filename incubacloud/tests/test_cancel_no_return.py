"""A stop and a step that cannot be stopped halfway, under real transactions.

«Update changed modules» of a rebuild commits module by module (Odoo's
``loading.py``; measured on 8-nov-2026): stopped halfway, the database is
left half updated under the image that runs. So the stop and that step are
decided under one advisory lock of the job: the executor, before the step,
takes it, reads the flag and marks the job (``cloud.job.no.return``); a
stop written on a started job takes it too and is refused once the mark is
there. Nothing of it is written on ``cloud_job`` from the executor's cursor,
whose row the job's own transaction stores at the end.

The cursors are real, as in ``test_success_before_cancel``: the fixture is
committed (and removed after), the stop is written on a cursor of its own,
and the executor's step runs on its durable cursor, in a thread of its own
when it has to wait for the stop's lock.
"""
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, api
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..models.abstract_executor import JobCancelled
from ..models.cloud_job_no_return import NO_RETURN_LOCK_NAMESPACE
from ..models.deploy_instance_executor import DeployInstanceExecutor
from ..models.host_probe_executor import HostProbeExecutor
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from ..models.transport import CommandResult, SSHTransport

#: The step the tests treat as past undoing.
_STEP = "Update changed modules"


class TestStopAgainstThePointOfNoReturn(TransactionCase):
    """A started job, a person's stop and the step it must not cut."""

    def setUp(self):
        """Commit a host and a started job on it."""
        super().setUp()
        self.registry = self.env.registry
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            host = env["cloud.host"].create({
                "name": "no-return-host", "ip_address": "192.0.2.93",
                "port": 22, "user": "root", "login_type": "ssh_key",
                "wildcard_domain": "no-return-host.example.com",
                "status": "compatible",
            })
            job_type = env["cloud.job.type"].search(
                [("code", "=", "host_probe")], limit=1,
            )
            job = env["cloud.job"].create({
                "name": "no-return-job", "host_id": host.id,
                "job_type_id": job_type.id,
            })
            cr.execute(
                "UPDATE cloud_job SET state = 'started' WHERE id = %s", (job.id,),
            )
            cr.commit()
            self.host_id, self.job_id = host.id, job.id
        self.addCleanup(self._remove_fixture)

    def _remove_fixture(self):
        """Delete the committed rows, children first."""
        with self.registry.cursor() as cr:
            for table in ("cloud_job_no_return", "cloud_job_log_chunk",
                          "cloud_audit_log", "cloud_alert"):
                cr.execute(f"DELETE FROM {table} WHERE job_id = %s", (self.job_id,))
            cr.execute("DELETE FROM cloud_job WHERE id = %s", (self.job_id,))
            for table in ("cloud_alert", "cloud_audit_log", "cloud_host_whitelist"):
                cr.execute(f"DELETE FROM {table} WHERE host_id = %s", (self.host_id,))
            cr.execute("DELETE FROM cloud_host WHERE id = %s", (self.host_id,))
            cr.commit()

    def _executor(self):
        """The job's executor, built on a cursor that sees the fixture.

        Its durable cursor is its own (``_durable_env``): the job record
        only lends it the registry and the id once built.
        """
        with self.registry.cursor() as cr:
            job = api.Environment(cr, SUPERUSER_ID, {})["cloud.job"].browse(
                self.job_id,
            )
            return HostProbeExecutor(job, job.host_id)

    def _state_and_mark(self):
        """``(state, step of the mark or None)``, as committed."""
        with self.registry.cursor() as cr:
            cr.execute("SELECT state FROM cloud_job WHERE id = %s", (self.job_id,))
            state = cr.fetchone()[0]
            cr.execute(
                "SELECT step FROM cloud_job_no_return WHERE job_id = %s",
                (self.job_id,),
            )
            row = cr.fetchone()
        return state, row and row[0]

    def _lock_waiters(self):
        """How many sessions wait for the job's stop lock."""
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                " AND NOT granted AND classid = %s AND objid = %s",
                (NO_RETURN_LOCK_NAMESPACE, self.job_id),
            )
            return cr.fetchone()[0]

    def test_a_stop_that_holds_the_lock_ends_the_job_before_the_step(self):
        """Stop first: the step waits for its commit, sees it, never starts.

        The step runs here and the stop's commit in a thread, once the
        step is seen waiting: an environment cannot be built outside the
        main thread while the modules load, a cursor can be committed.
        """
        executor = self._executor()
        seen = {}

        def _commit_once_waited_for(cr):
            """Commit the stop once the step waits for its lock."""
            deadline = time.monotonic() + 10
            while not self._lock_waiters() and time.monotonic() < deadline:
                time.sleep(0.05)
            seen["waited"] = bool(self._lock_waiters())
            cr.commit()

        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["cloud.job"].browse(self.job_id).cancel_job()
            env.flush_all()
            thread = threading.Thread(target=_commit_once_waited_for, args=(cr,))
            thread.start()
            try:
                with self.assertRaises(JobCancelled):
                    executor._pass_no_return(_STEP)
            finally:
                thread.join(15)
        self.assertTrue(seen.get("waited"), "the step did not wait for the stop")
        self.assertEqual(self._state_and_mark(), ("cancelled", None))

    def test_a_stop_after_the_step_started_is_refused(self):
        """Step first: a stop whose transaction began before the mark was
        committed still finds it, and the job runs on."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            job = env["cloud.job"].browse(self.job_id)
            # The request's snapshot, taken before the step.
            self.assertEqual(job.state, "started")
            self._executor()._pass_no_return(_STEP)
            with self.assertRaises(UserError) as caught:
                job.cancel_job()
            cr.rollback()
        self.assertIn(f"#{self.job_id}", str(caught.exception))
        self.assertIn(_STEP, str(caught.exception))
        self.assertEqual(self._state_and_mark(), ("started", _STEP))

    def test_every_write_of_the_flag_is_refused_past_the_step(self):
        """Not only ``cancel_job``: a plain write of the flag on a started
        job (a tenant's provisioning started again) meets the same lock."""
        self._executor()._pass_no_return(_STEP)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            with self.assertRaises(UserError):
                env["cloud.job"].browse(self.job_id).write({"state": "cancelled"})
            cr.rollback()
        self.assertEqual(self._state_and_mark(), ("started", _STEP))

    def test_a_stop_before_the_step_is_the_flag_it_always_was(self):
        """No mark: the flag is written, and the executor reads it."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["cloud.job"].browse(self.job_id).cancel_job()
            cr.commit()
        self.assertEqual(self._state_and_mark(), ("cancelled", None))
        with self.assertRaises(JobCancelled):
            self._executor()._pass_no_return(_STEP)
        self.assertEqual(self._state_and_mark(), ("cancelled", None))


class TestTheStepsAroundThePointOfNoReturn(TransactionCase):
    """Where the steps loop asks, and which executors have the point."""

    def test_the_step_is_decided_before_it_runs_and_a_stop_skips_it(self):
        """The lock is asked for right before the step, not for the others,
        and a stop found there means the step never reaches the host."""
        transport = MagicMock(spec=SSHTransport)

        async def _execute(*_args, **_kwargs):
            return CommandResult(stdout="", exit_status=0)

        transport.execute.side_effect = _execute
        job = self.env["cloud.job"].new({"name": "steps"})
        executor = HostProbeExecutor(job, self.env["cloud.host"])
        steps = [("Before", "true"), (_STEP, "update"), ("After", "true")]
        with patch.object(HostProbeExecutor, "_no_return_from", _STEP), patch.object(
            HostProbeExecutor, "_pass_no_return", autospec=True,
            side_effect=JobCancelled("stop"),
        ) as passing, patch.object(HostProbeExecutor, "_sys", autospec=True):
            with self.assertRaises(JobCancelled):
                asyncio.run(executor._run_steps(transport, steps))
        passing.assert_called_once_with(executor, _STEP)
        self.assertEqual(
            [call.args[0] for call in transport.execute.call_args_list], ["true"],
        )

    def test_a_rebuild_has_its_update_as_the_point_and_a_deploy_none(self):
        """A rebuild cannot undo its database update; a new instance
        stopped halfway is removed, so a deploy has no such step."""
        self.assertEqual(RebuildInstanceExecutor._no_return_from, _STEP)
        self.assertIsNone(DeployInstanceExecutor._no_return_from)
