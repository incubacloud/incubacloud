"""The end of a job that changes an instance has the instance probed at once."""

from unittest.mock import patch

from .test_cloud_job import _run_after_commit
from .test_job_queue import _QueueCase


class TestStateRefresh(_QueueCase):
    """A start, a stop, a restore… ended: the probe runs now, not at its turn."""

    def _probes(self, inst=None):
        """The health probes queued for *inst* (the fixture's by default)."""
        return self.Job.search([
            ("instance_id", "=", (inst or self.inst).id),
            ("job_type_id.code", "=", "instance_health"),
        ])

    def test_a_stop_that_ends_probes_its_instance(self):
        """Done or failed, a stop or a restart has its instance probed,
        once the end commits (the probe's row keys on the instance)."""
        for code, state in (("stop_instance", "done"), ("restart_instance", "failed")):
            with self.subTest(code=code):
                inst = self._instance(f"queue-{code.replace('_', '-')}")
                self._job(code, inst=inst).write({"state": state})
                self.assertFalse(self._probes(inst), "not before the commit")
                _run_after_commit(self.env)
                self.assertEqual(len(self._probes(inst)), 1)

    def test_what_changes_nothing_probes_nothing(self):
        """A backup that ends, or a stop cancelled before it ran, leaves the
        probe to its turn."""
        self._job("backup_create").write({"state": "done"})
        self._job("stop_instance", state="queued").write({"state": "cancelled"})
        _run_after_commit(self.env)
        self.assertFalse(self._probes())

    def test_an_instance_not_deployed_is_not_probed(self):
        """Nothing is on a server to probe."""
        draft = self._instance("queue-draft", state="draft")
        self.assertEqual(draft._refresh_state_now(), [])
        self.assertFalse(self._probes(draft))

    def test_the_probe_only_reads_the_state(self):
        """The probe of a job's end asks for the state alone, so the
        streaks and alerts keep counting the cron's cycles."""
        self._job("restart_instance").write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(self._probes().payload, {"state_only": True})

    def test_a_probe_waiting_to_start_is_not_asked_for_twice(self):
        """Two commands in a row: the probe that waits reads both."""
        first = self.inst._refresh_state_now()
        self.assertEqual(len(first), 1)
        self.assertEqual(self.Job.browse(first).state, "pending")
        self.assertEqual(self.inst._refresh_state_now(), [])
        self.Job.browse(first).write({"state": "done"})
        self.assertEqual(len(self.inst._refresh_state_now()), 1)

    def test_what_the_crons_leave_alone_is_not_probed(self):
        """An instance the layer keeps out of the periodic crons (one it
        keeps stopped on purpose) is not probed either."""
        with patch.object(
            type(self.inst),
            "_periodic_maintenance_domain",
            autospec=True,
            return_value=[("id", "!=", self.inst.id)],
        ):
            self.assertEqual(self.inst._refresh_state_now(), [])
        self.assertFalse(self._probes())

    def test_asking_for_the_probe_takes_no_queue_lock(self):
        """Asked for at a job's end, with that job's queue locks taken: a
        hidden job, outside the queues, so it waits behind nobody."""
        with patch.object(
            type(self.Job), "_lock_scopes", autospec=True,
        ) as lock:
            self.assertEqual(len(self.inst._refresh_state_now()), 1)
        lock.assert_not_called()
