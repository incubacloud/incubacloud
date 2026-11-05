"""What the job lists are told about why a job reads "Action required".

The chip says why, by the code of the alert that keeps the job and, for a
failure holding the queue, by what failed; and it leads to that alert.
``_format`` carries the three: ``action_alert_id``, ``action_alert_code``
and ``action_job_type_code``, for a job held behind a failure, a job
blocked on an alert, and a failed job with a conflict of its own.
"""
from .test_job_queue import _QueueCase


class TestTheReasonOfActionRequired(_QueueCase):

    def _row(self, job):
        return job._format()[0]

    def test_a_job_held_behind_a_failed_deploy_names_its_alert_and_type(self):
        _deploy, alert = self._failed_with_alert("deploy_instance")
        restart = self._enqueue("restart_instance")
        row = self._row(restart)
        self.assertTrue(row["requires_action"])
        self.assertEqual(row["queue_held_alert_id"], alert.id)
        self.assertEqual(row["action_alert_id"], alert.id)
        self.assertEqual(row["action_alert_code"], "job_failed")
        self.assertEqual(row["action_job_type_code"], "deploy_instance")

    def test_a_blocked_job_names_the_alert_it_waits_on(self):
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "conflict", instance=self.inst,
        )
        blocked = self._job("deploy_instance", state=False, blocked_alert_id=alert.id)
        row = self._row(blocked)
        self.assertTrue(row["requires_action"])
        self.assertEqual(row["action_alert_id"], alert.id)
        self.assertEqual(row["action_alert_code"], "addon_conflict")

    def test_a_failed_job_with_its_own_conflict_names_it(self):
        failed = self._job("deploy_instance", state="failed")
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "pip_conflict", "conflict", level="critical", instance=self.inst, job=failed,
        )
        row = self._row(failed)
        self.assertTrue(row["requires_action"])
        self.assertEqual(row["action_alert_id"], alert.id)
        self.assertEqual(row["action_alert_code"], "pip_conflict")
        self.assertEqual(row["action_job_type_code"], "deploy_instance")

    def test_a_job_that_needs_nobody_carries_none(self):
        row = self._row(self._job("restart_instance", state="done"))
        self.assertFalse(row["requires_action"])
        self.assertIsNone(row["action_alert_id"])
        self.assertIsNone(row["action_alert_code"])
        self.assertIsNone(row["action_job_type_code"])

    def test_a_retry_passing_the_hold_is_not_held(self):
        deploy, _alert = self._failed_with_alert("deploy_instance")
        self._job("rebuild_instance")
        retry = self.Job.browse(deploy.retry_job())
        row = self._row(retry)
        self.assertFalse(row["queue_held"])
        self.assertIsNone(row["action_alert_id"])
