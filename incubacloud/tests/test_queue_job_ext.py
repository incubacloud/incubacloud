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
