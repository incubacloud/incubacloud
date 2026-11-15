"""The server's restart through the queue: its lock, and its attempts.

``test_host_reboot`` calls the restart's hooks one by one. Here it runs as
the runner runs it: queue_job's ``Job.perform``, through
``cloud.job.execute``, then the store of what it raised, through
``queue_job_ext``. What keeps the hold while the job waits for its server
is the state the queue stores for a park with no checkpoint (``pending``,
one of ``_running_states``): a change there would let the hold go with the
server still down, and nothing else would say so.

The branch where the lock is taken needs a second real connection: an
advisory lock is granted again to the session that holds it, and in
registry test mode every cursor of the job rides on the test's.
"""
from unittest.mock import patch

import odoo
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.queue_job.exception import JobError, RetryableJobError
from odoo.addons.queue_job.job import Job as QueueJob

from ..models.abstract_executor import JobParked
from ..models.cloud_host_reboot import REBOOT_LOCK_NS, REBOOT_PANEL_KEY
from ..models.host_reboot_executor import (
    EXIT_VETO,
    PARK_FIRST_S,
    PARK_RETRY_S,
    HostRebootExecutor,
)
from .test_cloud_job import _run_after_commit
from .test_host_reboot import _BOOT, _NEW_BOOT, _RebootJobCase, _transport


@tagged("post_install", "-at_install")
class TestRebootHoldLock(_RebootJobCase):
    """A dispatch that holds the shared lock keeps the restart from holding."""

    def _shared(self, key):
        """Take the shared lock of *key* on a connection of its own.

        As the dispatcher does while it starts a job of the host
        (``cloud.job._reboot_hold_of``); let go when the test ends.

        :param int key: a host's id, or ``REBOOT_PANEL_KEY``
        """
        cr = odoo.sql_db.db_connect(self.env.cr.dbname).cursor()
        self.addCleanup(cr.close)
        self.addCleanup(cr.rollback)
        cr.execute(
            "SELECT pg_try_advisory_xact_lock_shared(%s, %s)", (REBOOT_LOCK_NS, key),
        )
        self.assertTrue(cr.fetchone()[0], "the second connection takes the lock")

    def _take(self, job):
        """Run ``_take_hold`` of *job*; return its reasons."""
        return self._executor(HostRebootExecutor, job)._take_hold(_BOOT)

    def test_a_dispatch_on_the_host_keeps_the_restart_from_holding_it(self):
        job = self._reboot_job()
        self._shared(self.host.id)
        reasons = self._take(job)
        self.assertEqual(len(reasons), 1)
        self.assertIn("starting or runs there", reasons[0])
        self.assertFalse(self._row(job))

    def test_a_dispatch_on_another_host_does_not(self):
        job = self._reboot_job()
        other, _inst = self._other_host()
        self._shared(other.id)
        self.assertEqual(self._take(job), [])
        self.assertTrue(self._row(job))

    def test_on_the_panel_s_own_server_any_dispatch_keeps_it(self):
        job = self._reboot_job()
        self._shared(REBOOT_PANEL_KEY)
        with patch(
            "odoo.addons.incubacloud.models.host_reboot_executor.panel_boot_id",
            return_value=_BOOT,
        ):
            reasons = self._take(job)
        self.assertTrue(reasons)
        self.assertFalse(self._row(job))


@tagged("post_install", "-at_install")
class TestRebootThroughTheQueue(_RebootJobCase):
    """The attempts of a restart as the runner makes and stores them."""

    def setUp(self):
        super().setUp()
        self.job = self._enqueue("host_reboot", inst=False)
        self.assertEqual(self.job.state, "pending")
        self.uuid = self.job.queue_job_id.uuid

    def _attempt(self, transport=None, error=None):
        """One attempt, run and stored as the runner does; return what it raised.

        :param transport: what ``get_transport`` yields
        :param Exception error: what ``get_transport`` raises instead
        """
        stored = QueueJob.load(self.env, self.uuid)
        stored.set_started()
        stored.store()
        self.env.flush_all()
        raised = None
        with self._connect(transport, error):
            try:
                stored.perform()
            except RetryableJobError as exc:
                raised = exc
                # The runner's ``retry_postpone``.
                stored.postpone(result=str(exc), seconds=exc.seconds)
                stored.set_pending(reset_retry=False)
            except JobError as exc:
                raised = exc
                stored.set_failed(exc_info=str(exc))
            else:
                stored.set_done()
        stored.store()
        self.env.flush_all()
        self.env.invalidate_all()
        return raised

    def _orders(self, transport):
        return [c.args[0] for c in transport.execute.call_args_list]

    def test_the_park_with_no_checkpoint_keeps_the_hold_until_the_server_is_back(self):
        first = _transport()
        parked = self._attempt(first)
        self.assertIsInstance(parked, JobParked)
        self.assertEqual(parked.seconds, PARK_FIRST_S)
        # Stored as a postpone: the cloud job ``pending``, which holds.
        self.assertEqual(self.job.queue_job_id.state, "pending")
        self.assertEqual(self.job.state, "pending")
        self.assertFalse(self.env["cloud.job.checkpoint"].search_count(
            [("job_id", "=", self.job.id)],
        ))
        self.assertEqual(self.Job._reboot_hold_of([self.host.id], lock=False), self.job.id)
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        # Past its point of no return, a stop is refused in ``pending`` too.
        with self.assertRaises(UserError) as caught:
            self.job.cancel_job()
        self.assertIn("cannot be stopped any more", str(caught.exception))

        # The server is not back yet: parked again, still holding.
        waiting = self._attempt(error=ConnectionRefusedError())
        self.assertIsInstance(waiting, JobParked)
        self.assertEqual(waiting.seconds, PARK_RETRY_S)
        self.assertEqual(self.job.state, "pending")
        self.assertEqual(self.Job._reboot_hold_of([self.host.id], lock=False), self.job.id)

        # Back: the attempt waits for the server, asks no veto and gives
        # no order; the job ends done and its end serves what it held.
        back = _transport(boot=_NEW_BOOT)
        self.assertIsNone(self._attempt(back))
        orders = self._orders(back)
        self.assertFalse(any("host_reboot.sh guard" in o for o in orders))
        self.assertFalse(any("host_reboot.sh reboot" in o for o in orders))
        self.assertTrue(any("host_reboot.sh verify" in o for o in orders))
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.Job._reboot_hold_of([self.host.id], lock=False), 0)
        self.assertEqual(self.host.reboot_boot_id, _NEW_BOOT)
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_a_veto_ends_the_job_done_saying_why(self):
        transport = _transport(guard=(EXIT_VETO, "IC_VETO=pg_dump is running\n"))
        self.assertIsNone(self._attempt(transport))
        self.assertEqual(self.job.state, "done")
        self.assertTrue(self._said(self.job, "Not restarted: The server said no"))
        self.assertFalse(any("host_reboot.sh reboot" in o for o in self._orders(transport)))
        self.assertFalse(self.env["cloud.job.no.return"].search_count(
            [("job_id", "=", self.job.id)],
        ))
        self.assertEqual(self.Job._reboot_hold_of([self.host.id], lock=False), 0)

    def test_the_end_of_the_panel_s_own_restart_serves_every_queue(self):
        with patch(
            "odoo.addons.incubacloud.models.host_reboot_executor.panel_boot_id",
            return_value=_BOOT,
        ):
            self.assertIsInstance(self._attempt(_transport()), JobParked)
        other, inst = self._other_host()
        waiting = self.Job.browse(self.Job.enqueue(other.id, inst.id, "restart_instance"))
        self.assertEqual(waiting.state, "queued")
        self.assertIsNone(self._attempt(_transport(boot=_NEW_BOOT)))
        _run_after_commit(self.env)
        self.assertEqual(waiting.state, "pending")
