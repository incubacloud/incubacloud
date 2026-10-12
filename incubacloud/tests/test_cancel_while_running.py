"""A job a person cancels while it runs ends cancelled, not failed.

queue_job cannot stop a started job, so ``cancel_job`` sets a flag the
executor polls and the executor stops by raising — which queue_job
stores as a failure. A customer who cancelled a rebuild to try the
button got a failure alert and a "failed" mail, then a "Resolved" one
when his next rebuild finished (12-oct-2026). Stored as a cancellation
from then on, the stop still reached queue_job as an exception, which
queue_job logs with its traceback: the log scraper raised an
``instance_error_logs`` alert on the customer's panel for it. ``execute``
now returns the stop instead.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.abstract_executor import (
    CANCELLED_WHILE_RUNNING,
    JobCancelled,
)
from odoo.addons.incubacloud.models.host_probe_executor import HostProbeExecutor


class TestCancelWhileRunning(TransactionCase):

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env['cloud.host'].create({
            'name': 'cancel-host',
            'ip_address': '10.0.0.61',
            'user': 'ubuntu',
            'wildcard_domain': 'cancel.example.com',
        })
        self.cjob, self.qjob = self._started('uuid-cancel-running')

    def _started(self, uuid, graph_uuid=False, state='started'):
        """A cloud job and its queue job, in *state*.

        :return: ``(cloud.job, queue.job)``.
        """
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'host_probe')], limit=1,
        ) or self.env['cloud.job.type'].create({
            'name': 'host_probe', 'code': 'host_probe', 'apply_to': 'host',
        })
        qjob = self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': f'qj-{uuid}',
            'state': state,
            'graph_uuid': graph_uuid,
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': job_type.id,
            'name': f'Job {uuid}',
            'queue_job_uuid': uuid,
            'queue_job_id': qjob.id,
        })
        return cjob, qjob

    def _queue_job_stores_the_stop(self, qjob):
        """What queue_job writes when the executor raised on the flag."""
        qjob.write({
            'state': 'failed',
            'exc_name': 'odoo.addons.queue_job.exception.JobError',
            'exc_message': 'Job cancelled',
            'exc_info': 'Traceback (most recent call last): ... Job cancelled',
        })
        self.env['cloud.job'].invalidate_model(['state'])

    def _failure_alerts(self, cjob):
        return self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('code', '=', 'job_failed'),
        ])

    def test_the_executor_stops_with_its_own_exception(self):
        self.cjob.cancel_job()
        self.env.flush_all()
        executor = HostProbeExecutor(self.cjob, self.host)
        with self.assertRaises(JobCancelled):
            executor._check_cancel()

    def test_a_job_cancelled_while_it_ran_ends_cancelled(self):
        self.assertEqual(self.cjob.state, 'started')
        self.cjob.cancel_job()
        self.env.flush_all()
        self._queue_job_stores_the_stop(self.qjob)
        self.assertEqual(self.qjob.state, 'cancelled')
        self.assertTrue(self.qjob.date_cancelled)
        self.assertFalse(self.qjob.exc_info)
        self.assertEqual(self.cjob.state, 'cancelled')
        self.assertFalse(
            self._failure_alerts(self.cjob),
            "a job the customer stopped must not raise a failure alert",
        )

    def test_a_failure_nobody_asked_for_is_still_a_failure(self):
        self._queue_job_stores_the_stop(self.qjob)
        self.assertEqual(self.qjob.state, 'failed')
        self.assertEqual(self.cjob.state, 'failed')
        self.assertTrue(self._failure_alerts(self.cjob))

    def test_what_waits_on_it_is_cancelled_too(self):
        cjob, qjob = self._started('uuid-head', graph_uuid='graph-cancel')
        waiting, waiting_q = self._started(
            'uuid-tail', graph_uuid='graph-cancel', state='wait_dependencies',
        )
        cjob.cancel_job()
        self.env.flush_all()
        self._queue_job_stores_the_stop(qjob)
        waiting_q.invalidate_recordset(['state'])
        self.assertEqual(waiting.state, 'cancelled')
        self.assertEqual(waiting_q.state, 'cancelled')
        self.assertFalse(self._failure_alerts(waiting))

    def test_execute_returns_the_stop_without_a_traceback(self):
        with patch.object(
            HostProbeExecutor, 'pre_run_checks', autospec=True,
        ), patch.object(
            HostProbeExecutor, '_publish_bus', autospec=True,
        ), patch.object(
            HostProbeExecutor, 'run', autospec=True,
            side_effect=JobCancelled('Job cancelled'),
        ), self.assertNoLogs('odoo.addons.incubacloud', level='ERROR'):
            result = self.cjob.execute()
        self.assertEqual(result, CANCELLED_WHILE_RUNNING)

    def test_a_stop_stored_as_done_ends_cancelled(self):
        self.cjob.cancel_job()
        self.env.flush_all()
        # What queue_job writes once ``execute`` returned the stop.
        self.qjob.write({
            'state': 'done',
            'result': CANCELLED_WHILE_RUNNING,
            'date_done': '2026-10-13 00:00:00',
        })
        self.env['cloud.job'].invalidate_model(['state'])
        self.assertEqual(self.qjob.state, 'cancelled')
        self.assertTrue(self.qjob.date_cancelled)
        self.assertFalse(self.qjob.date_done)
        self.assertEqual(self.cjob.state, 'cancelled')
        self.assertFalse(self._failure_alerts(self.cjob))

    def test_a_job_that_finished_as_cancel_was_pressed_stays_done(self):
        self.cjob.cancel_job()
        self.env.flush_all()
        # The executor ended before it saw the flag: no marker.
        self.qjob.write({'state': 'done', 'result': False})
        self.env['cloud.job'].invalidate_model(['state'])
        self.assertEqual(self.qjob.state, 'done')
        self.assertEqual(self.cjob.state, 'done')
