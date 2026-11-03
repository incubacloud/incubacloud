"""A rebuild the instance's crons hold back comes back, for 30 minutes.

``cron_guard.sh`` exits 75 when the instance still had crons executing
after its budget: nothing was updated. Every rebuild is queued with
``max_retries=1``, so the plain ``RetryableJobError`` it raised was
queue_job's last attempt, and the job ended failed with an alert, a
mail and a held queue (inventory H4). It now comes back every five
minutes without spending its attempt, and fails for good only after 30
minutes counted from its first attempt. Five, not one: each attempt
builds the image again from scratch.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields

from odoo.addons.queue_job.exception import FailedJobError, RetryableJobError
from odoo.addons.queue_job.job import Job as QueueJob

from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from .test_cloud_job import _run_after_commit
from .test_job_queue import _QueueCase

_BUSY = {"Update changed modules": {"stdout": "", "exit_status": 75}}
_GAVE_UP = (
    "The instance's crons kept running for 30 minutes; nothing was updated."
)


class TestCronsBusy(_QueueCase):

    def setUp(self):
        super().setUp()
        self.job = self._enqueue("rebuild_instance")
        self.assertTrue(self.job.queue_job_id)

    def _executor(self):
        """The rebuild's executor on the queued job."""
        return RebuildInstanceExecutor(job_record=self.job, host_record=self.host)

    def _chunk(self, minutes_ago):
        """A log line of the job written *minutes_ago*, as a durable cursor would."""
        chunk = self.env["cloud.job.log.chunk"].sudo().create({
            "job_id": self.job.id, "content": "Connecting...", "source": "system",
        })
        stamp = fields.Datetime.now() - timedelta(minutes=minutes_ago)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_job_log_chunk SET create_date = %s WHERE id = %s",
            (stamp, chunk.id),
        )
        chunk.invalidate_recordset(["create_date"])
        return chunk

    def _created(self, minutes_ago):
        """Date the job's queue.job as created *minutes_ago*."""
        qjob = self.job.queue_job_id
        self.env.flush_all()
        # queue_job refuses the field through the ORM.
        self.env.cr.execute(
            "UPDATE queue_job SET date_created = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(minutes=minutes_ago), qjob.id),
        )
        qjob.invalidate_recordset(["date_created"])

    def test_the_first_attempt_comes_back_in_five_minutes_without_spending_it(self):
        with self.assertRaises(RetryableJobError) as caught:
            self._executor().parse_results(_BUSY)
        self.assertEqual(caught.exception.seconds, 300)
        self.assertTrue(caught.exception.ignore_retry)

    def test_a_later_attempt_inside_the_half_hour_comes_back_too(self):
        self._created(40)
        self._chunk(29)
        with self.assertRaises(RetryableJobError):
            self._executor().parse_results(_BUSY)

    def test_after_half_an_hour_the_rebuild_fails_and_says_why(self):
        self._created(40)
        self._chunk(31)
        self._chunk(5)
        errors = self._executor().parse_results(_BUSY)
        self.assertEqual(errors, [_GAVE_UP])

    def test_notes_from_before_the_dispatch_do_not_count(self):
        """A note logged while the job waited its turn is not an attempt."""
        self._chunk(120)
        self._created(2)
        with self.assertRaises(RetryableJobError):
            self._executor().parse_results(_BUSY)

    def test_through_queue_job_it_is_postponed_not_failed(self):
        """``Job.perform`` with ``max_retries=1``: no ``FailedJobError``.

        Before the fix the first exit 75 reached the retry budget and
        queue_job turned it into ``Max. retries (1) reached``.
        """

        def run(executor):
            """Answer as a rebuild whose update met busy crons."""
            executor._outcome_errors(_BUSY, ["Restart"])

        stored = QueueJob.load(self.env, self.job.queue_job_id.uuid)
        self.assertEqual(stored.max_retries, 1)
        stored.set_started()
        stored.store()
        self.env.flush_all()
        retry = None
        with patch.object(
            RebuildInstanceExecutor, "pre_run_checks", autospec=True,
        ), patch.object(
            RebuildInstanceExecutor, "blocking_alert", autospec=True,
            return_value=self.env["cloud.alert"],
        ), patch.object(
            RebuildInstanceExecutor, "run", autospec=True, side_effect=run,
        ):
            try:
                stored.perform()
            except FailedJobError as exc:
                self.fail(f"the job failed for good: {exc}")
            except RetryableJobError as exc:
                retry = exc
        self.assertIsNotNone(retry, "the job was not postponed")
        self.assertEqual(stored.retry, 0)
        self.assertEqual(retry.seconds, 300)
        # What the runner's ``retry_postpone`` does next.
        stored.postpone(result=str(retry), seconds=retry.seconds)
        stored.set_pending(reset_retry=False)
        stored.store()
        self.assertEqual(self.job.state, "pending")
        self.assertFalse(self.env["cloud.alert"].search([
            ("job_id", "=", self.job.id), ("state", "=", "active"),
        ]))
        # Still the head of the instance's queue: what comes next waits.
        _run_after_commit(self.env)
        waiting = self._enqueue("restart_instance")
        _run_after_commit(self.env)
        self.assertEqual(waiting.state, "queued")
