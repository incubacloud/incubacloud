"""The end of a job that changes an instance has the instance probed at once."""

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
        """Done or failed, a stop or a restart has its instance probed."""
        for code, state in (("stop_instance", "done"), ("restart_instance", "failed")):
            with self.subTest(code=code):
                before = len(self._probes())
                self._job(code).write({"state": state})
                self.assertEqual(len(self._probes()), before + 1)

    def test_what_changes_nothing_probes_nothing(self):
        """A backup that ends, or a stop cancelled before it ran, leaves the
        probe to its turn."""
        self._job("backup_create").write({"state": "done"})
        self._job("stop_instance", state="queued").write({"state": "cancelled"})
        self.assertFalse(self._probes())

    def test_an_instance_not_deployed_is_not_probed(self):
        """Nothing is on a server to probe."""
        draft = self._instance("queue-draft", state="draft")
        self.assertEqual(draft._refresh_state_now(), [])
        self.assertFalse(self._probes(draft))
