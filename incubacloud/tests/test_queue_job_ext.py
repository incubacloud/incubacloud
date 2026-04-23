"""Integration test for queue_job_ext.QueueJob.write.

Specifically verifies that the raw UPDATE performed on the cloud_job
table invalidates the ORM cache for ``state`` — otherwise subsequent
reads in the same transaction (broadcast, notifications, computes)
would see the previous value.
"""
from odoo.tests.common import TransactionCase


class TestQueueJobExtInvalidatesCache(TransactionCase):

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'qj-host',
            'ip_address': '10.0.0.42',
            'user': 'ubuntu',
            'wildcard_domain': 'qj.example.com',
        })
        self.jt = self._job_type('qj_test')
        self.cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self.jt.id,
            'name': 'QJ Test',
            'queue_job_uuid': 'test-uuid-1',
        })

    def test_terminal_state_transition_invalidates_cache(self):
        """After queue.job.write({'state': 'done'}), a later read of
        cloud.job.state in the same transaction returns the fresh
        value. Regression guard for the raw UPDATE bypass."""
        # Seed the queue.job with a non-terminal state so our write
        # transitions it to 'done' and triggers the ext hook.
        qjob = self.env['queue.job'].sudo().create({
            'uuid': 'test-uuid-1',
            'name': 'qj-test',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        # Prime the ORM cache with the stale value.
        self.assertEqual(self.cjob.state, 'pending')

        # queue_job_ext.write runs: does raw UPDATE on cloud_job,
        # then invalidate_model(['state']).
        qjob.write({'state': 'done'})

        # If invalidate_model is missing, the cache would still
        # return 'pending'. With the fix it yields 'done'.
        self.assertEqual(self.cjob.state, 'done')


class TestQueueJobExtFailureAlert(TransactionCase):
    """Failure of a queue.job opens a ``cloud.alert`` so the failure
    lingers in the Alerts panel beyond the one-shot bus toast."""

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'alert-host',
            'ip_address': '10.0.0.43',
            'user': 'ubuntu',
            'wildcard_domain': 'alert.example.com',
        })

    def _make(self, code, uuid, apply_to='host'):
        jt = self._job_type(code, apply_to=apply_to)
        cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': f'Job {code}',
            'queue_job_uuid': uuid,
        })
        qjob = self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': f'qj-{code}',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        return cjob, qjob

    def test_failure_creates_warning_alert_for_non_severe_type(self):
        cjob, qjob = self._make('host_probe', 'uuid-warn')
        qjob.write({
            'state': 'failed',
            'exc_message': 'ssh: Network is unreachable',
        })
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('code', '=', 'job_failed'),
        ])
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'warning')
        self.assertEqual(alert.state, 'active')
        self.assertIn('host_probe', alert.message.lower())
        self.assertIn('Network is unreachable', alert.message)

    def test_failure_creates_critical_alert_for_severe_type(self):
        cjob, qjob = self._make('deploy_instance', 'uuid-crit')
        qjob.write({
            'state': 'failed',
            'exc_message': 'docker: image pull timeout',
        })
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('code', '=', 'job_failed'),
        ])
        self.assertEqual(alert.level, 'critical')

    def test_failure_in_hidden_type_does_not_create_alert(self):
        """Background cron types (host_metrics, docker_prune, …)
        self-recover and would flood the panel; never alerted."""
        cjob, qjob = self._make('host_metrics', 'uuid-hidden')
        qjob.write({
            'state': 'failed', 'exc_message': 'transient failure',
        })
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id),
        ])
        self.assertFalse(alert)

    def test_retry_success_dismisses_previous_alert(self):
        """When the same cloud.job reaches 'done' after a failure the
        active job_failed alert is auto-dismissed."""
        cjob, qjob = self._make('rebuild_instance', 'uuid-retry')
        qjob.write({'state': 'failed', 'exc_message': 'oops'})
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('state', '=', 'active'),
        ])
        self.assertEqual(len(alert), 1)

        # Reuse the same cloud.job on retry — flip state back to
        # enqueued and then to done, same uuid.
        qjob.write({'state': 'enqueued'})
        qjob.write({'state': 'done'})

        alert.invalidate_recordset()
        self.assertEqual(alert.state, 'dismissed')
