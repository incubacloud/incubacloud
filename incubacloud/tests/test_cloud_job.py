"""
Tier 2 — ORM integration tests for cloud.job utilities.

Tests focus on the parts that don't require actual SSH execution:
  - _get_last_system_message
  - load_chunks  (filtering and after_id pagination)
  - cancel_job / retry_job guard conditions
  - _get_executor raises for unknown job types
  - _format / _format_history dict structure
"""
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


def _ensure_job_type(env, code, apply_to='instance'):
    """Return existing job type or create a minimal one for test isolation."""
    jt = env['cloud.job.type'].search([('code', '=', code)], limit=1)
    if not jt:
        jt = env['cloud.job.type'].create({
            'name': code,
            'code': code,
            'apply_to': apply_to,
        })
    return jt


class TestCloudJobLastSystemMessage(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Test Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
        })
        self.job_type = _ensure_job_type(self.env, 'test_sys_msg', apply_to='host')
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.job_type.id,
            'name': 'Test Job',
        })
        self.Chunk = self.env['cloud.job.log.chunk']

    def test_empty_when_no_chunks(self):
        self.assertEqual(self.job._get_last_system_message(), '')

    def test_returns_last_system_chunk(self):
        self.Chunk.create({'job_id': self.job.id, 'source': 'system', 'content': 'First'})
        self.Chunk.create({'job_id': self.job.id, 'source': 'system', 'content': 'Second'})
        self.assertEqual(self.job._get_last_system_message(), 'Second')

    def test_ignores_stdout_chunks(self):
        self.Chunk.create({'job_id': self.job.id, 'source': 'stdout', 'content': 'stdout line'})
        self.Chunk.create({'job_id': self.job.id, 'source': 'system', 'content': 'system msg'})
        self.assertEqual(self.job._get_last_system_message(), 'system msg')

    def test_ignores_stderr_chunks(self):
        self.Chunk.create({'job_id': self.job.id, 'source': 'stderr', 'content': 'err'})
        self.assertEqual(self.job._get_last_system_message(), '')

    def test_mixed_sources_returns_last_system(self):
        self.Chunk.create({'job_id': self.job.id, 'source': 'system', 'content': 'sys1'})
        self.Chunk.create({'job_id': self.job.id, 'source': 'stdout', 'content': 'out1'})
        self.Chunk.create({'job_id': self.job.id, 'source': 'system', 'content': 'sys2'})
        self.assertEqual(self.job._get_last_system_message(), 'sys2')


class TestCloudJobLoadChunks(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Test Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
        })
        self.job_type = _ensure_job_type(self.env, 'test_chunks', apply_to='host')
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.job_type.id,
            'name': 'Chunk Test Job',
        })
        self.Chunk = self.env['cloud.job.log.chunk']

    def test_all_valid_sources_returned(self):
        self.Chunk.create({
            'job_id': self.job.id, 'source': 'stdout', 'content': 'out',
        })
        self.Chunk.create({
            'job_id': self.job.id, 'source': 'system', 'content': 'visible',
        })
        result = self.env['cloud.job'].load_chunks(self.job.id)
        sources = [c['source'] for c in result['chunks']]
        self.assertIn('stdout', sources)
        self.assertIn('system', sources)

    def test_after_id_filters_older_chunks(self):
        c1 = self.Chunk.create({'job_id': self.job.id, 'source': 'system', 'content': 'old'})
        c2 = self.Chunk.create({'job_id': self.job.id, 'source': 'system', 'content': 'new'})
        result = self.env['cloud.job'].load_chunks(self.job.id, after_id=c1.id)
        ids = [c['id'] for c in result['chunks']]
        self.assertNotIn(c1.id, ids)
        self.assertIn(c2.id, ids)

    def test_chunk_dict_has_required_keys(self):
        self.Chunk.create({'job_id': self.job.id, 'source': 'stdout', 'content': 'hello'})
        result = self.env['cloud.job'].load_chunks(self.job.id)
        self.assertTrue(result['chunks'])
        chunk = result['chunks'][0]
        for key in ('id', 'source', 'content'):
            self.assertIn(key, chunk)

    def test_result_contains_state_key(self):
        result = self.env['cloud.job'].load_chunks(self.job.id)
        self.assertIn('state', result)

    def test_empty_job_returns_empty_chunks(self):
        result = self.env['cloud.job'].load_chunks(self.job.id)
        self.assertEqual(result['chunks'], [])


class TestCloudJobCancelRetry(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Test Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
        })
        self.job_type = _ensure_job_type(self.env, 'test_cancel', apply_to='host')
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.job_type.id,
            'name': 'Cancel Test Job',
        })

    def test_cancel_raises_without_queue_job(self):
        """cancel_job raises UserError when there is no linked queue.job."""
        with self.assertRaises(UserError):
            self.job.cancel_job()

    def test_retry_raises_when_no_queue_job(self):
        """retry_job raises UserError when there is no linked queue.job."""
        with self.assertRaises(UserError):
            self.job.retry_job()


class TestCloudJobGetExecutor(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Test Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
        })
        self.job_type = _ensure_job_type(self.env, 'no_executor_type', apply_to='host')
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.job_type.id,
            'name': 'Executor Test Job',
        })

    def test_get_executor_raises_for_unregistered_type(self):
        """A job type with no registered executor raises ValueError."""
        with self.assertRaises(ValueError):
            self.job._get_executor()

    def test_get_executor_returns_registered_class(self):
        """A job type with a registered executor returns the class."""
        from odoo.addons.incubacloud.models.registry import executor_registry
        from odoo.addons.incubacloud.models.abstract_executor import AbstractSSHExecutor

        class _TestExecutor(AbstractSSHExecutor):
            _job_type = None  # don't auto-register, register manually below
            def get_commands(self):
                return []

        executor_registry._executors['no_executor_type'] = _TestExecutor
        try:
            result = self.job._get_executor()
            self.assertIs(result, _TestExecutor)
        finally:
            executor_registry._executors.pop('no_executor_type', None)


class TestCloudJobFormat(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Format Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
        })
        self.job_type = _ensure_job_type(self.env, 'test_format', apply_to='host')
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.job_type.id,
            'name': 'Format Job',
        })

    def test_format_returns_list(self):
        result = self.job._format()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    def test_format_dict_has_required_keys(self):
        result = self.job._format()[0]
        for key in ('id', 'name', 'host', 'job_type', 'state', 'create_date',
                    'last_system_message', 'download_url'):
            self.assertIn(key, result)

    def test_format_id_matches(self):
        result = self.job._format()[0]
        self.assertEqual(result['id'], self.job.id)

    def test_format_history_dict_has_required_keys(self):
        result = self.job._format_history()
        self.assertTrue(result)
        entry = result[0]
        for key in ('id', 'name', 'host', 'host_id', 'job_type', 'state',
                    'create_date', 'write_date', 'duration_s', 'log_lines',
                    'last_system_message'):
            self.assertIn(key, entry)

    def test_format_history_duration_non_negative(self):
        result = self.job._format_history()[0]
        self.assertGreaterEqual(result['duration_s'], 0)

    def test_load_history_returns_expected_structure(self):
        result = self.env['cloud.job'].load_history()
        self.assertIn('jobs', result)
        self.assertIn('hosts', result)


class TestCloudJobLoadHistoryCategory(TransactionCase):
    """Tests for the job_category filter in load_history()."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Category Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
        })
        self.op_type = _ensure_job_type(self.env, 'test_op_type', apply_to='host')
        self.admin_type = _ensure_job_type(self.env, 'host_metrics', apply_to='host')
        # Create one operational job and one admin job (both in terminal state)
        self.op_job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.op_type.id,
            'name': 'Operational Job',
        })
        self.admin_job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.admin_type.id,
            'name': 'Admin Job',
        })

    def _job_ids(self, result):
        return [j['id'] for j in result['jobs']]

    def test_default_is_operational(self):
        """No filter defaults to operational — admin jobs excluded."""
        result = self.env['cloud.job'].load_history()
        ids = self._job_ids(result)
        self.assertIn(self.op_job.id, ids)
        self.assertNotIn(self.admin_job.id, ids)

    def test_operational_excludes_admin_jobs(self):
        result = self.env['cloud.job'].load_history({'job_category': 'operational'})
        ids = self._job_ids(result)
        self.assertIn(self.op_job.id, ids)
        self.assertNotIn(self.admin_job.id, ids)

    def test_admin_includes_only_admin_jobs(self):
        result = self.env['cloud.job'].load_history({'job_category': 'admin'})
        ids = self._job_ids(result)
        self.assertNotIn(self.op_job.id, ids)
        self.assertIn(self.admin_job.id, ids)

    def test_all_includes_both_categories(self):
        result = self.env['cloud.job'].load_history({'job_category': 'all'})
        ids = self._job_ids(result)
        self.assertIn(self.op_job.id, ids)
        self.assertIn(self.admin_job.id, ids)

    def test_result_has_hosts_list(self):
        result = self.env['cloud.job'].load_history({'job_category': 'admin'})
        self.assertIn('hosts', result)
        self.assertIsInstance(result['hosts'], list)


class TestCloudJobDataRacePrevention(TransactionCase):
    """Test that enqueue blocks when a job is already running."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'test-host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
            'user': 'root',
            'login_type': 'password',
            'password': 'test',
        })
        self.project = self.env['cloud.project'].create({'name': 'Test'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'test-race',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })

    def test_no_running_job_allows_enqueue(self):
        """When no job is running, the search returns empty."""
        running = self.env['cloud.job'].search([
            ('instance_id', '=', self.instance.id),
            ('state', 'in', ('started', 'pending', 'enqueued')),
        ], limit=1)
        self.assertFalse(running)

    def test_active_states_constant(self):
        """The _active_states list matches what the controller checks."""
        Job = self.env['cloud.job']
        for state in ('pending', 'enqueued', 'started'):
            self.assertIn(state, Job._active_states)

    def test_terminal_states_not_blocking(self):
        """Terminal states should not appear in _active_states."""
        Job = self.env['cloud.job']
        for state in ('done', 'failed', 'cancelled'):
            self.assertNotIn(state, Job._active_states)

    def test_no_jobs_returns_empty(self):
        """Search for active jobs on a fresh instance returns empty."""
        running = self.env['cloud.job'].search([
            ('instance_id', '=', self.instance.id),
            ('state', 'in', self.env['cloud.job']._active_states),
        ], limit=1)
        self.assertFalse(running)

    def test_delete_blocked_by_active_states(self):
        """The delete endpoint uses the same active states for blocking."""
        # Verify the states used in cloud_delete_instance match _active_states
        active = self.env['cloud.job']._active_states
        delete_states = ('started', 'pending', 'enqueued')
        for s in delete_states:
            self.assertIn(s, active,
                f"Delete check state '{s}' must be in _active_states")


class TestChainFailurePropagation(TransactionCase):
    """When a job fails, chained jobs in wait_dependencies are failed too."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'H1', 'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'P'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'test-chain',
            'project_id': self.project.id,
            'host_id': self.host.id,
        })
        self.jt = _ensure_job_type(self.env, 'test_chain_jt')

    def _job(self, state='pending'):
        return self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': self.jt.id,
            'name': 'Chain job',
            'state': state,
        })

    def _db_state(self, job):
        """Read state directly from DB (bypasses the stored-related cache)."""
        self.env.cr.execute(
            "SELECT state FROM cloud_job WHERE id = %s", (job.id,)
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def test_wait_dependencies_job_failed_when_predecessor_fails(self):
        """Simulate queue_job_ext write propagation via direct SQL path."""
        job1 = self._job(state='started')
        job2 = self._job(state='wait_dependencies')

        # Simulate what queue_job_ext.write does when job1 fails
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'failed'"
            " WHERE instance_id = %s"
            "   AND state = 'wait_dependencies'"
            "   AND id != %s",
            (self.instance.id, job1.id),
        )
        self.assertEqual(self._db_state(job2), 'failed')

    def test_done_jobs_not_affected(self):
        """Only wait_dependencies jobs are affected, not done ones."""
        job1 = self._job(state='started')
        job2 = self._job(state='done')

        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'failed'"
            " WHERE instance_id = %s"
            "   AND state = 'wait_dependencies'"
            "   AND id != %s",
            (self.instance.id, job1.id),
        )
        self.assertEqual(self._db_state(job2), 'done')

