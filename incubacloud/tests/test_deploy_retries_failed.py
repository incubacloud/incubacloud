"""Deploy after a failed deploy retries it.

A failed deploy holds its instance's queue until it is retried
successfully or its ``job_failed`` alert is dismissed. Pressing Deploy
again queued a new deploy behind that hold, where it waited for good
while the panel said "Queued". Now Deploy retries the failed deploy —
the Retry button's own path: ``retry_of_id``, its role check, its place
at the head of the queue past the hold — and its success resolves the
alert. The hold stays in every other case: a conflict alert asks for a
person, and a dismissed alert holds nothing to retry.
"""
from odoo.exceptions import UserError

from .test_cloud_job import _run_after_commit
from .test_job_queue import _QueueCase


class TestDeployRetriesTheFailedDeploy(_QueueCase):

    def setUp(self):
        super().setUp()
        self.draft = self._instance("queue-draft", state="draft")

    def _deploys(self):
        return self.Job.search([
            ("instance_id", "=", self.draft.id),
            ("job_type_id.code", "=", "deploy_instance"),
        ], order="id")

    def test_deploy_retries_the_failure_and_its_success_resolves_the_alert(self):
        failed, alert = self._failed_with_alert("deploy_instance", inst=self.draft)
        job = self.Job.browse(self.draft.deploy())
        self.assertEqual(job.retry_of_id, failed)
        self.assertTrue(job.queue_head)
        self.assertEqual(self._deploys(), failed | job)
        self.assertEqual(self.draft.state, "deploying")
        _run_after_commit(self.env)
        self.assertEqual(job.state, "pending", "the retry passes the hold")
        job.queue_job_id.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(alert.resolved_by_job_id, job)

    def test_the_deploy_pressed_is_in_the_audit_log(self):
        self._failed_with_alert("deploy_instance", inst=self.draft)
        job_id = self.draft.deploy()
        logs = self.env["cloud.audit.log"].sudo().search([("job_id", "=", job_id)])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs.action, "Deploy Instance")
        self.assertEqual(logs.instance_id, self.draft)

    def test_a_second_press_while_the_retry_waits_is_refused(self):
        self._failed_with_alert("deploy_instance", inst=self.draft)
        self.draft.deploy()
        with self.assertRaises(UserError):
            self.draft.deploy()
        self.assertEqual(len(self._deploys()), 2)

    def test_a_dismissed_alert_leaves_a_new_deploy(self):
        failed, alert = self._failed_with_alert("deploy_instance", inst=self.draft)
        alert.write({"state": "dismissed"})
        job = self.Job.browse(self.draft.deploy())
        self.assertFalse(job.retry_of_id)
        self.assertNotEqual(job, failed)

    def test_a_conflict_open_on_the_failure_keeps_the_hold(self):
        """Beside its job_failed (a warning) or in its place (critical)."""
        for level in ("warning", "critical"):
            with self.subTest(level=level):
                inst = self._instance(f"queue-conflict-{level}", state="draft")
                failed = self._job("deploy_instance", state="failed", inst=inst)
                self.env["cloud.alert"].sudo().raise_alert(
                    "pip_conflict", "conflict", level=level, instance=inst, job=failed,
                )
                self.Job._create_job_failed_alert(failed, exc_message="boom")
                self.assertTrue(self.Job._queue_held_by(inst.id, None))
                job = self.Job.browse(inst.deploy())
                self.assertFalse(job.retry_of_id)
                self.assertNotEqual(job, failed)

    def test_a_failure_of_another_type_is_not_what_deploy_retries(self):
        self._failed_with_alert("restore_instance", inst=self.draft)
        job = self.Job.browse(self.draft.deploy())
        self.assertFalse(job.retry_of_id)
        self.assertEqual(job.state, "queued", "held behind the restore")

    def test_the_failure_is_found_by_the_last_deploy(self):
        """An older failed deploy whose later retry is the one failing."""
        first, _alert = self._failed_with_alert("deploy_instance", inst=self.draft)
        second = self._job(
            "deploy_instance", state="failed", inst=self.draft, retry_of_id=first.id,
        )
        self.Job._create_job_failed_alert(second, exc_message="boom again")
        job = self.Job.browse(self.draft.deploy())
        self.assertEqual(job.retry_of_id, second)
