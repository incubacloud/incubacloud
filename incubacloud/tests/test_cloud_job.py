"""
Tier 2 — ORM integration tests for cloud.job utilities.

Tests focus on the parts that don't require actual SSH execution:
  - _get_last_system_message
  - load_chunks  (filtering and after_id pagination)
  - cancel_job / retry_job guard conditions
  - _get_executor raises for unknown job types
  - _format / _format_history dict structure
  - _webhook_fields trigger-aware payload serialization
"""
import base64
from datetime import timedelta

from odoo.tests.common import BaseCase, TransactionCase, new_test_user
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models.cloud_job import _webhook_fields


def _run_after_commit(env):
    """Run what *env*'s transaction left for after its commit.

    The job queues are served there (``cloud.job._schedule_dispatch``),
    on a cursor of their own; a test's transaction never commits, so the
    test runs it. The writes are flushed first, as a commit would, and
    the cache is dropped after. The test must be in registry test mode
    (``registry_enter_test_mode``): outside it that cursor is a real
    connection, which sees nothing of the test and waits for its locks.
    """
    env.flush_all()
    env.cr.postcommit.run()
    env.invalidate_all()


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

    def test_cancel_queued_job_cancels_underlying_queue_job(self):
        """Cancelling a queued (not-started) job routes through the
        queue.job (button_cancelled) so the runner skips it, instead of
        the state write being clobbered when the runner starts it."""
        from unittest.mock import patch
        qjob = self.env['queue.job'].sudo().create({
            'uuid': 'cancel-queued-uuid',
            'name': 'qj-cancel', 'state': 'enqueued',
            'method_name': 'load_jobs', 'model_name': 'cloud.job',
            'func_string': 'cloud.job().load_jobs()',
        })
        self.job.write({'queue_job_uuid': 'cancel-queued-uuid'})
        self.job.invalidate_recordset(['queue_job_id', 'state'])
        self.assertEqual(self.job.state, 'enqueued')
        with patch.object(
            type(self.job.queue_job_id), 'button_cancelled',
        ) as mock_cancel:
            self.job.cancel_job()
        mock_cancel.assert_called_once()


class TestEnqueueJobTypeGate(TransactionCase):
    """The public enqueue entrypoint must enforce a per-type role gate so
    a consultant cannot enqueue host-destructive jobs by direct RPC.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Optional modules may add NOT NULL columns to res_partner without
        # defaults; new_test_user() creates a partner without those fields.
        # Patch a safe SQL default so the users can be created regardless
        # of what is installed in the snapshot DB.
        cls.env.cr.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'res_partner'
               AND is_nullable = 'NO'
               AND column_default IS NULL
               AND column_name NOT IN ('id', 'name', 'company_type',
                                       'type', 'lang', 'active',
                                       'create_uid', 'write_uid',
                                       'create_date', 'write_date')
        """)
        for col, dtype in cls.env.cr.fetchall():
            default = "''" if 'char' in dtype or 'text' in dtype else "'no'"
            cls.env.cr.execute(
                f'ALTER TABLE res_partner '
                f'ALTER COLUMN "{col}" SET DEFAULT {default}'
            )

    def setUp(self):
        super().setUp()
        from odoo.exceptions import AccessError
        self.AccessError = AccessError
        self.Job = self.env['cloud.job']
        self.consultant = new_test_user(
            self.env, login='ic_consultant_gate',
            groups='base.group_user,incubacloud.group_cloud_consultant',
        )
        self.developer = new_test_user(
            self.env, login='ic_developer_gate',
            groups='base.group_user,incubacloud.group_cloud_developer',
        )
        self.manager = new_test_user(
            self.env, login='ic_manager_gate',
            groups='base.group_user,incubacloud.group_cloud_manager',
        )

    #: What each role may ask for. Anything not listed for a role must
    #: raise; the lists mirror ``_job_type_min_group``.
    _CONSULTANT_OK = (
        'deploy_instance', 'rebuild_instance',
        'start_instance', 'stop_instance', 'restart_instance',
    )
    _DEVELOPER_ONLY = (
        'backup_list', 'backup_create', 'backup_download',
        'backup_download_neutralized', 'backup_restore',
        'restore_instance', 'export_instance',
    )
    _MANAGER_ONLY = (
        'delete_host', 'full_setup', 'delete_project', 'host_probe',
        'host_hardening', 'docker_prune', 'setup_whitelist',
        'move_cutover', 'move_cleanup_source', 'move_rollback_cleanup',
        'install_observability', 'deploy_metrics_central',
    )

    def test_consultant_allowed_lifecycle_types(self):
        job = self.Job.with_user(self.consultant)
        for code in self._CONSULTANT_OK:
            with self.subTest(code=code):
                job._check_job_type_allowed(code)

    def test_consultant_blocked_from_developer_types(self):
        job = self.Job.with_user(self.consultant)
        for code in self._DEVELOPER_ONLY:
            with self.subTest(code=code), self.assertRaises(self.AccessError):
                job._check_job_type_allowed(code)

    def test_consultant_blocked_from_manager_types(self):
        job = self.Job.with_user(self.consultant)
        for code in self._MANAGER_ONLY:
            with self.subTest(code=code), self.assertRaises(self.AccessError):
                job._check_job_type_allowed(code)

    def test_developer_allowed_developer_types(self):
        job = self.Job.with_user(self.developer)
        for code in self._DEVELOPER_ONLY + self._CONSULTANT_OK:
            with self.subTest(code=code):
                job._check_job_type_allowed(code)

    def test_developer_blocked_from_manager_types(self):
        # The move chain rewrites host_id / wipes the source directory
        # with no guard of its own, so it must stay manager-only.
        job = self.Job.with_user(self.developer)
        for code in self._MANAGER_ONLY:
            with self.subTest(code=code), self.assertRaises(self.AccessError):
                job._check_job_type_allowed(code)

    def test_manager_allowed_everything(self):
        job = self.Job.with_user(self.manager)
        for code in self._CONSULTANT_OK + self._DEVELOPER_ONLY + self._MANAGER_ONLY:
            with self.subTest(code=code):
                job._check_job_type_allowed(code)

    def test_unknown_type_fails_closed(self):
        # A job type nobody mapped must require the manager role rather
        # than fall through unguarded.
        with self.assertRaises(self.AccessError):
            self.Job.with_user(self.developer)._check_job_type_allowed(
                'no_such_job_type',
            )
        self.Job.with_user(self.manager)._check_job_type_allowed(
            'no_such_job_type',
        )

    def test_sudo_bypasses_gate(self):
        # Internal callers (crons, executors) use sudo and must pass.
        self.Job.sudo()._check_job_type_allowed('delete_host')
        self.Job.sudo()._check_job_type_allowed('no_such_job_type')

    # -- delete_instance: production escalates to manager ----------------

    def _instance(self, environment):
        project = self.env['cloud.project'].create({
            'name': f'gate-proj-{environment}',
            'member_ids': [(4, self.consultant.id), (4, self.developer.id)],
        })
        return self.env['cloud.instance'].create({
            'name': f'gate-inst-{environment}',
            'project_id': project.id,
            'environment': environment,
        })

    def test_consultant_may_delete_non_production_instance(self):
        inst = self._instance('staging')
        self.Job.with_user(self.consultant)._check_job_type_allowed(
            'delete_instance', inst.id,
        )

    def test_consultant_cannot_delete_production_instance(self):
        # The remote teardown runs before the record is unlinked, so the
        # gate has to fire here and not only in ``unlink``.
        inst = self._instance('production')
        with self.assertRaises(self.AccessError):
            self.Job.with_user(self.consultant)._check_job_type_allowed(
                'delete_instance', inst.id,
            )

    def test_developer_cannot_delete_production_instance(self):
        inst = self._instance('production')
        with self.assertRaises(self.AccessError):
            self.Job.with_user(self.developer)._check_job_type_allowed(
                'delete_instance', inst.id,
            )

    def test_manager_may_delete_production_instance(self):
        inst = self._instance('production')
        self.Job.with_user(self.manager)._check_job_type_allowed(
            'delete_instance', inst.id,
        )

    def test_foreign_production_instance_cannot_be_deleted(self):
        # End-to-end pin of the rule-alignment invariant: for an instance
        # OUTSIDE the caller's projects, ``rule_instance_member`` hides it
        # from the escalation check — the enqueue must still fail, because
        # ``rule_job_member`` scopes job creation by the same membership.
        # If someone ever widens one rule without the other, this test is
        # what catches it.
        project = self.env['cloud.project'].create({'name': 'foreign-proj'})
        inst = self.env['cloud.instance'].create({
            'name': 'foreign-prod',
            'project_id': project.id,
            'environment': 'production',
        })
        host = self.env['cloud.host'].create({
            'name': 'foreign-host',
            'ip_address': '10.0.0.78',
            'user': 'root',
            'wildcard_domain': 'foreign.example.com',
        })
        before = self.Job.sudo().search_count([])
        with self.assertRaises(self.AccessError):
            self.Job.with_user(self.consultant).enqueue(
                host.id, inst.id, 'delete_instance',
            )
        self.assertEqual(self.Job.sudo().search_count([]), before)

    def test_unblock_applies_the_same_gate(self):
        # ``unblock_and_enqueue`` dispatches under the unblocking user's
        # env, so it carries the same role gate as ``enqueue``.
        inst = self._instance('staging')
        host = self.env['cloud.host'].create({
            'name': 'unblock-gate-host',
            'ip_address': '10.0.0.79',
            'user': 'root',
            'wildcard_domain': 'unblock.example.com',
        })
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'backup_create')], limit=1,
        )
        job = self.Job.create({
            'host_id': host.id,
            'instance_id': inst.id,
            'job_type_id': job_type.id,
            'name': 'Create Backup',
        })
        # The gate is the first check, so no blocked-alert setup is needed.
        with self.assertRaises(self.AccessError):
            job.with_user(self.consultant).unblock_and_enqueue()

    def test_retry_applies_the_same_gate(self):
        # A failed higher-role job visible to a consultant (instance of
        # their project) must not be relaunchable by them: the retry runs
        # under the retrying user's env.
        inst = self._instance('staging')
        host = self.env['cloud.host'].create({
            'name': 'retry-gate-host',
            'ip_address': '10.0.0.77',
            'user': 'root',
            'wildcard_domain': 'retry.example.com',
        })
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'backup_create')], limit=1,
        )
        job = self.Job.create({
            'host_id': host.id,
            'instance_id': inst.id,
            'job_type_id': job_type.id,
            'name': 'Create Backup',
        })
        job.write({'state': 'failed'})
        with self.assertRaises(self.AccessError):
            job.with_user(self.consultant).retry_job()
        # The developer (the role that may ask for backups) can retry it.
        job.invalidate_recordset(['state'])
        retried = job.with_user(self.developer).retry_job()
        self.assertTrue(retried)

    def test_enqueue_refuses_before_creating_the_job(self):
        # The gate must run before any record is written, so a refused
        # call leaves no orphan cloud.job behind.
        inst = self._instance('staging')
        before = self.Job.sudo().search_count([])
        with self.assertRaises(self.AccessError):
            self.Job.with_user(self.consultant).enqueue(
                inst.host_id.id, inst.id, 'backup_create',
            )
        self.assertEqual(self.Job.sudo().search_count([]), before)


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
        self.assertIn('instances', result)
        self.assertIn('users', result)
        self.assertIsInstance(result['instances'], list)


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
    """The states that make a target busy, as every reader counts them."""

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

    def test_queued_is_active_but_not_running(self):
        """A job waiting in its queue occupies the target without running."""
        Job = self.env['cloud.job']
        self.assertIn('queued', Job._active_states)
        self.assertNotIn('queued', Job._running_states)
        for state in Job._running_states:
            self.assertIn(state, Job._active_states)


class TestEnqueueBypassRunningCheck(TransactionCase):
    """``bypass_running_check=True`` means "part of the job running now".

    An executor enqueueing a follow-up from its own ``on_success`` — when
    the parent cloud.job is still ``started`` — puts it at the head of
    the instance's queue, before anything a user queued meanwhile; a
    plain enqueue waits its turn behind them."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'bypass-host',
            'ip_address': '10.0.0.1',
            'user': 'root',
            'wildcard_domain': 'bypass.test.local',
            'login_type': 'password',
            'password': 'x',
        })
        self.project = self.env['cloud.project'].create({'name': 'P'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'test-bypass',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        self.registry_enter_test_mode()
        self.parent_jt = _ensure_job_type(self.env, 'test_parent_jt')
        self.follow_jt = _ensure_job_type(self.env, 'test_follow_jt')
        # Mimic an executor mid-on_success: parent job already started.
        self.parent = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.parent_jt.id,
            'instance_id': self.instance.id,
            'name': 'Parent',
            'state': 'started',
        })

    def test_default_waits_behind_the_parent(self):
        new_id = self.env['cloud.job'].enqueue(
            self.host.id, self.instance.id, 'test_follow_jt',
        )
        new_job = self.env['cloud.job'].browse(new_id)
        self.assertEqual(new_job.state, 'queued')
        self.assertFalse(new_job.queue_head)
        self.assertFalse(new_job.queue_job_uuid)

    def test_bypass_goes_to_the_head_and_starts_when_the_parent_ends(self):
        user_id = self.env['cloud.job'].enqueue(
            self.host.id, self.instance.id, 'test_follow_jt',
        )
        new_id = self.env['cloud.job'].enqueue(
            self.host.id, self.instance.id, 'test_follow_jt',
            payload={'follow': True},
            bypass_running_check=True,
        )
        new_job = self.env['cloud.job'].browse(new_id)
        self.assertTrue(new_job.exists())
        self.assertEqual(new_job.job_type_id, self.follow_jt)
        self.assertEqual(new_job.state, 'queued')
        self.assertTrue(new_job.queue_head)
        self.parent.write({'state': 'done'})
        _run_after_commit(self.env)
        self.assertEqual(new_job.state, 'pending')
        self.assertEqual(self.env['cloud.job'].browse(user_id).state, 'queued')


class TestHostScopedRacePrevention(TransactionCase):
    """Host-only jobs (no instance_id) must serialise per host the same
    way instance jobs serialise per instance: one runs, the next waits
    in the host's queue. Regression: the serialisation used to apply
    only when ``instance_id`` was set, so clicking "Verify host" while a
    setup/probe was already running launched a parallel job."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'race-host',
            'ip_address': '10.0.0.9',
            'user': 'root',
            'wildcard_domain': 'race.test.local',
            'login_type': 'password',
            'password': 'x',
        })
        self.project = self.env['cloud.project'].create({'name': 'P'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'race-inst',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        self.registry_enter_test_mode()
        self.host_jt = _ensure_job_type(self.env, 'test_host_jt', apply_to='host')
        self.host_jt2 = _ensure_job_type(self.env, 'test_host_jt2', apply_to='host')
        self.inst_jt = _ensure_job_type(self.env, 'test_inst_jt')
        # A hidden background job type (its code is in _hidden_job_types).
        self.hidden_jt = _ensure_job_type(
            self.env, 'host_metrics', apply_to='host',
        )

    def _start(self, job):
        """Put ``job`` in the ``started`` state."""
        job.write({'state': 'started'})

    def _make(self, job_type, instance=False):
        """Create a cloud.job for the host (optionally instance-scoped)."""
        return self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': instance and instance.id,
            'job_type_id': job_type.id,
            'name': job_type.code,
        })

    def _enqueued(self, instance, code):
        """Enqueue *code* on the host (or *instance*); return the job."""
        job_id = self.env['cloud.job'].enqueue(
            self.host.id, instance and instance.id, code,
        )
        return self.env['cloud.job'].browse(job_id)

    def test_active_host_job_queues_second_host_job(self):
        """A second host-only job waits in the host's queue."""
        running = self._make(self.host_jt)
        self._start(running)
        second = self._enqueued(False, 'test_host_jt2')
        self.assertEqual(second.state, 'queued')
        _run_after_commit(self.env)
        self.assertEqual(second.state, 'queued')
        running.write({'state': 'done'})
        _run_after_commit(self.env)
        self.assertEqual(second.state, 'pending')

    def test_no_active_host_job_allows_enqueue(self):
        """With no active host job, the host job starts at once."""
        self.assertEqual(self._enqueued(False, 'test_host_jt').state, 'pending')

    def test_host_job_does_not_block_instance_job(self):
        """An active host-only job does not hold an instance job back."""
        self._start(self._make(self.host_jt))
        self.assertEqual(
            self._enqueued(self.instance, 'test_inst_jt').state, 'pending',
        )

    def test_instance_job_does_not_block_host_job(self):
        """An active instance job does not hold a host-only job back."""
        self._start(self._make(self.inst_jt, instance=self.instance))
        self.assertEqual(self._enqueued(False, 'test_host_jt').state, 'pending')

    def test_hidden_host_job_does_not_block(self):
        """A hidden background host job (host_metrics) occupies nothing."""
        self._start(self._make(self.hidden_jt))
        self.assertEqual(self._enqueued(False, 'test_host_jt').state, 'pending')


class TestRetryAndUnblockRespectTheGuard(TransactionCase):
    """``retry_job`` and ``unblock_and_enqueue`` go through the queue.

    Both used to dispatch straight to ``with_delay`` without taking the
    advisory lock or looking for an active job, so a retry (or a
    pip-conflict resolution) fired while a deploy was already running on
    the target — two SSH sessions mutating the same instance at once.
    They wait their turn now like anything ``enqueue`` creates: the
    retry at the head of the queue, the unblocked job in the place its
    creation gives it — it was asked for before what was queued while it
    waited for its alert.
    """

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'guard-host',
            'ip_address': '10.0.0.31',
            'user': 'root',
            'wildcard_domain': 'guard.test.local',
        })
        self.project = self.env['cloud.project'].create({'name': 'GP'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'guard-inst',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        self.registry_enter_test_mode()
        self.jt = _ensure_job_type(self.env, 'test_guard_jt')

    def _job(self, state):
        """Create an instance job in ``state``."""
        return self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': self.jt.id,
            'name': 'guard job',
            'state': state,
        })

    def test_retry_waits_at_the_head_while_another_job_runs(self):
        failed = self._job('failed')
        running = self._job('started')
        retry = self.env['cloud.job'].browse(failed.retry_job())
        self.assertEqual(retry.state, 'queued')
        self.assertTrue(retry.queue_head)
        self.assertFalse(retry.queue_job_uuid)
        _run_after_commit(self.env)
        self.assertEqual(retry.state, 'queued')
        running.write({'state': 'done'})
        _run_after_commit(self.env)
        self.assertEqual(retry.state, 'pending')
        self.assertTrue(retry.queue_job_uuid)

    def test_retry_works_when_the_target_is_idle(self):
        """The ordinary retry path starts once its transaction commits."""
        failed = self._job('failed')
        retry = self.env['cloud.job'].browse(failed.retry_job())
        _run_after_commit(self.env)
        self.assertEqual(retry.state, 'pending')

    def test_a_failed_job_does_not_block_its_own_retry(self):
        """``failed`` is not an active state — retrying must not self-lock."""
        failed = self._job('failed')
        self.assertTrue(failed.retry_job())

    def test_unblock_waits_while_another_job_runs(self):
        blocked = self._job(False)
        running = self._job('started')
        blocked.unblock_and_enqueue()
        self.assertEqual(blocked.state, 'queued')
        self.assertFalse(blocked.queue_job_uuid)
        _run_after_commit(self.env)
        self.assertEqual(blocked.state, 'queued')
        running.write({'state': 'done'})
        _run_after_commit(self.env)
        self.assertEqual(blocked.state, 'pending')

    def test_unblock_works_when_the_target_is_idle(self):
        blocked = self._job(False)
        blocked.unblock_and_enqueue()
        _run_after_commit(self.env)
        self.assertTrue(blocked.queue_job_uuid)


class TestChainFailurePropagation(TransactionCase):
    """When a job fails, the steps of its chain still waiting are cancelled."""

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

    def _db_state(self, job):
        """Read the state from the row: the steps are ended with SQL."""
        self.env.cr.execute(
            "SELECT state FROM cloud_job WHERE id = %s", (job.id,)
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _chained_job(self, instance, state, graph_uuid, uuid):
        """Create a cloud.job on *instance* backed by a queue.job in a graph.

        :returns: the ``cloud.job``, linked to its ``queue.job``.
        """
        self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': uuid,
            'state': state,
            'method_name': 'execute',
            'model_name': 'cloud.job',
            'func_string': 'execute()',
            'graph_uuid': graph_uuid,
        })
        return self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': instance.id,
            'job_type_id': self.jt.id,
            'name': uuid,
            'queue_job_uuid': uuid,
        })

    def _db_queue_state(self, job):
        """Read the state of *job*'s queue.job directly from the DB."""
        self.env.cr.execute(
            "SELECT state FROM queue_job WHERE uuid = %s",
            (job.queue_job_uuid,),
        )
        return self.env.cr.fetchone()[0]

    def test_a_chain_across_instances_is_cancelled_through_its_graph(self):
        """A staging built from production downloads on *production*.

        Production, 2026-10-03: the staging's deploy failed, the restore
        on the staging was cancelled, and the download on production
        waited forever — holding production's job slot, so nothing else
        could run on it.
        """
        source = self.env['cloud.instance'].create({
            'name': 'test-chain-source',
            'project_id': self.project.id,
            'host_id': self.host.id,
        })
        deploy = self._chained_job(
            self.instance, 'started', 'graph-a', 'graph-a-1',
        )
        download = self._chained_job(
            source, 'wait_dependencies', 'graph-a', 'graph-a-2',
        )
        unrelated = self._chained_job(
            source, 'wait_dependencies', 'graph-b', 'graph-b-1',
        )

        deploy.queue_job_id.write({'state': 'failed'})

        # Only the step that failed is a failure: the download never ran.
        self.assertEqual(self._db_state(download), 'cancelled')
        self.assertEqual(self._db_queue_state(download), 'cancelled')
        self.assertEqual(self._db_state(unrelated), 'wait_dependencies')
        self.assertEqual(
            self._db_queue_state(unrelated), 'wait_dependencies',
        )


class TestLoadHistoryAccessControl(TransactionCase):
    """Non-Job-Queue-Manager cloud users must be able to open the SPA's
    "View all activity" panel.  It calls ``cloud.job.load_history`` and
    used to trip ``Access Error: 'Queue Job' (queue.job)`` because
    ``_format_history`` reached into ``queue_job_id.date_done`` for the
    end-time of terminal jobs — that read needs
    ``queue_job.group_queue_job_manager``, which regular cloud users
    (Stakeholder/Consultant/Developer) do not have.  Storing
    ``date_done`` as a related field side-steps the ACL.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Optional modules may add NOT NULL columns to res_partner
        # without SQL defaults.  ``new_test_user`` creates a partner
        # without those fields → constraint violation.  Patch any
        # such column with a safe SQL default so the test does not
        # need to know which modules are installed.  Same workaround
        # used in TestCloudSecurityMixin.
        cls.env.cr.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'res_partner'
               AND is_nullable = 'NO'
               AND column_default IS NULL
               AND column_name NOT IN ('id', 'name', 'company_type',
                                       'type', 'lang', 'active',
                                       'create_uid', 'write_uid',
                                       'create_date', 'write_date')
        """)
        for col, dtype in cls.env.cr.fetchall():
            default = "''" if 'char' in dtype or 'text' in dtype else "'no'"
            cls.env.cr.execute(
                f'ALTER TABLE res_partner '
                f'ALTER COLUMN "{col}" SET DEFAULT {default}'
            )

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'ACL Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'test.example.com',
        })
        self.job_type = _ensure_job_type(
            self.env, 'test_acl_job', apply_to='host',
        )
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.job_type.id,
            'name': 'ACL Job',
        })
        # Project Manager (not Stakeholder/Consultant) so the user
        # passes ``rule_job_all`` and sees host-level jobs without
        # needing project membership.  PMs in this project do *not*
        # get ``queue_job.group_queue_job_manager`` (only the bot
        # user does — see res_users_ext._incubacloud_assign_bot_user).
        self.user = new_test_user(
            self.env,
            login='cloud_pm@test',
            groups='base.group_user,incubacloud.group_cloud_project_manager',
        )

    def test_user_lacks_queue_job_manager_group(self):
        # Sanity: confirm we're testing the right scenario.
        manager_group = self.env.ref('queue_job.group_queue_job_manager')
        self.assertNotIn(self.user, manager_group.user_ids)

    def test_load_history_does_not_raise_for_cloud_user(self):
        result = (
            self.env['cloud.job']
            .with_user(self.user)
            .load_history()
        )
        self.assertIn('jobs', result)

    def test_format_history_reads_terminal_end_without_queue_job_acl(self):
        # Force the terminal branch of _format_history without going
        # through queue_job (the recompute would itself need ACL).
        # ``state`` is a stored related; updating it directly via SQL
        # mirrors what queue_job_ext does at runtime.
        self.env.cr.execute(
            "UPDATE cloud_job SET state='done' WHERE id=%s",
            (self.job.id,),
        )
        self.env['cloud.job'].invalidate_model(['state'])

        result = (
            self.env['cloud.job']
            .with_user(self.user)
            .load_history()
        )
        ids = [j['id'] for j in result['jobs']]
        self.assertIn(self.job.id, ids)


class TestCronCleanupBackupAttachments(TransactionCase):
    """The cron has two jobs:

    Pass A — purge ``cloud.instance.backup`` rows older than 2h together
    with their attachment, so the SPA never shows a row whose Download
    button can no longer resolve.

    Pass B — purge orphan attachments produced by one-shot download
    jobs (``BackupDownloadExecutor``, ``BackupDownloadNeutralizedExecutor``)
    that live on ``cloud.job`` and never got linked to a row.
    """

    def setUp(self):
        super().setUp()
        from odoo import fields
        self.fields = fields
        self.host = self.env['cloud.host'].create({
            'name': 'cron-host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'test.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'cron-proj'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'cron-inst',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        self.job_type = _ensure_job_type(
            self.env, 'test_cron_cleanup', apply_to='instance',
        )
        self.cloud_job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': self.job_type.id,
            'name': 'Backup job',
        })
        self.now = self.fields.Datetime.now()
        self.expired = self.now - timedelta(hours=3)
        self.fresh = self.now - timedelta(minutes=30)

    # ── helpers ───────────────────────────────────────────────────

    def _attachment(self, name='cron-inst-backup-20260101.zip'):
        return self.env['ir.attachment'].create({
            'name': name,
            'type': 'binary',
            'datas': base64.b64encode(b'dummy').decode('ascii'),
            'res_model': 'cloud.job',
            'res_id': self.cloud_job.id,
        })

    def _row(self, attachment, backup_time):
        return self.env['cloud.instance.backup'].create({
            'instance_id': self.instance.id,
            'kind': 'archive',
            'backup_type': 'Full',
            'backup_time': backup_time,
            'attachment_id': attachment.id if attachment else False,
        })

    # ── Pass A ────────────────────────────────────────────────────

    def test_passA_purges_expired_row_and_attachment_together(self):
        att = self._attachment()
        row = self._row(att, self.expired)
        self.env['cloud.job'].cron_cleanup_backup_attachments()
        self.assertFalse(row.exists(), "expired row must be unlinked")
        self.assertFalse(att.exists(), "expired attachment must be unlinked")

    def test_passA_keeps_fresh_rows(self):
        att = self._attachment()
        row = self._row(att, self.fresh)
        self.env['cloud.job'].cron_cleanup_backup_attachments()
        self.assertTrue(row.exists(), "fresh row must survive")
        self.assertTrue(att.exists(), "fresh attachment must survive")

    def test_passA_handles_already_missing_attachment(self):
        # Manual cleanup may have removed the attachment first; the
        # row still has a stored attachment_id at the SQL level until
        # the trigger fires. Simulate by creating + unlinking the
        # attachment, leaving the FK column dangling, then running
        # the cron — it must not raise.
        att = self._attachment()
        row = self._row(att, self.expired)
        att.unlink()
        # The ondelete='set null' on attachment_id clears the FK on
        # the row, so search filter `attachment_id != False` filters
        # this row OUT — that is the correct behaviour: pass A only
        # owns rows whose attachment is still alive.  The row should
        # therefore survive pass A; pass B does nothing on cloud.job
        # since the attachment is already gone.  The cron must not
        # raise either way.
        self.env['cloud.job'].cron_cleanup_backup_attachments()
        self.assertTrue(row.exists())

    # ── Pass B ────────────────────────────────────────────────────

    def test_passB_purges_orphan_backup_attachment(self):
        # ``BackupDownloadExecutor`` writes attachments named
        # ``<inst>-backup-<ts>-<suffix>.zip`` directly on cloud.job
        # without creating a cloud.instance.backup row.
        orphan = self._attachment('cron-inst-backup-20260101-dump.zip')
        # Force expiry: ``create_date`` is auto-managed, so write SQL.
        self.env.cr.execute(
            "UPDATE ir_attachment SET create_date=%s WHERE id=%s",
            (self.expired, orphan.id),
        )
        orphan.invalidate_recordset(['create_date'])
        self.env['cloud.job'].cron_cleanup_backup_attachments()
        self.assertFalse(orphan.exists())

    def test_passB_purges_orphan_neutralized_attachment(self):
        orphan = self._attachment('cron-inst-neutralized-20260101-dump.zip')
        self.env.cr.execute(
            "UPDATE ir_attachment SET create_date=%s WHERE id=%s",
            (self.expired, orphan.id),
        )
        orphan.invalidate_recordset(['create_date'])
        self.env['cloud.job'].cron_cleanup_backup_attachments()
        self.assertFalse(orphan.exists())

    def test_passB_does_not_purge_attachment_referenced_by_a_row(self):
        # Pass A handles row+attachment together; pass B must not
        # race it on a still-referenced attachment.
        att = self._attachment()
        # Force the attachment expired at SQL level even though the
        # row's backup_time is fresh — pass A would skip, pass B
        # could otherwise grab it via the LIKE.
        self.env.cr.execute(
            "UPDATE ir_attachment SET create_date=%s WHERE id=%s",
            (self.expired, att.id),
        )
        att.invalidate_recordset(['create_date'])
        row = self._row(att, self.fresh)
        self.env['cloud.job'].cron_cleanup_backup_attachments()
        self.assertTrue(att.exists(),
                        "attachment still referenced by a row must survive")
        self.assertTrue(row.exists())


class TestCreateBackupPayloadPropagation(TransactionCase):
    """``cloud.instance.create_backup(with_filestore=...)`` must forward
    the kwarg to the ``backup_create`` step (where the executor reads
    it) and NOT to the trailing ``backup_list`` step (irrelevant there).
    Coercion to ``bool()`` happens at the service-layer boundary.
    """

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'pp-host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'test.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'pp-proj'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'pp-inst',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
            'state': 'deployed',
        })
        # Make sure both job types exist so enqueue_chain doesn't fail.
        _ensure_job_type(self.env, 'backup_create', apply_to='instance')
        _ensure_job_type(self.env, 'backup_list', apply_to='instance')

    def _chain_jobs(self, last_id):
        # ``enqueue_chain`` returns the trailing backup_list job id;
        # walk backwards via instance to find the create job created
        # in the same call.
        list_job = self.env['cloud.job'].browse(last_id)
        create_job = self.env['cloud.job'].search([
            ('instance_id', '=', self.instance.id),
            ('id', '!=', list_job.id),
            ('job_type_id.code', '=', 'backup_create'),
        ], order='id desc', limit=1)
        return create_job, list_job

    def test_with_filestore_true_lands_on_backup_create_step(self):
        last_id = self.instance.create_backup(with_filestore=True)
        create_job, list_job = self._chain_jobs(last_id)
        self.assertEqual(
            (create_job.payload or {}).get('with_filestore'),
            True,
        )
        # backup_list step does not need the flag.
        self.assertFalse(list_job.payload)

    def test_with_filestore_false_lands_on_backup_create_step(self):
        last_id = self.instance.create_backup(with_filestore=False)
        create_job, _ = self._chain_jobs(last_id)
        self.assertEqual(
            (create_job.payload or {}).get('with_filestore'),
            False,
        )

    def test_truthy_string_collapses_to_true(self):
        # Defensive: any truthy JSON-RPC value must collapse to a
        # plain bool before reaching the chain payload.
        last_id = self.instance.create_backup(
            with_filestore='--malicious; rm -rf /',
        )
        create_job, _ = self._chain_jobs(last_id)
        self.assertIs(
            (create_job.payload or {}).get('with_filestore'),
            True,
        )


class TestBackupContentsLabel(TransactionCase):
    """``_backup_contents_label`` is a pure helper used by the
    backups serializer.  Pin the three label cases so the SPA list
    stays consistent.
    """

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'lbl-host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'test.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'lbl-proj'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'lbl-inst',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        self.cloud_job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': _ensure_job_type(
                self.env, 'lbl_create', apply_to='instance',
            ).id,
            'name': 'Backup',
        })

    def _row(self, attachment_id, with_filestore):
        from odoo import fields
        return self.env['cloud.instance.backup'].create({
            'instance_id': self.instance.id,
            'kind': 'archive',
            'backup_type': 'Full',
            'backup_time': fields.Datetime.now(),
            'attachment_id': attachment_id,
            'with_filestore': with_filestore,
        })

    def _att(self):
        return self.env['ir.attachment'].create({
            'name': 'lbl-inst-backup-x.zip',
            'type': 'binary',
            'datas': base64.b64encode(b'x' * 100).decode('ascii'),
            'res_model': 'cloud.job',
            'res_id': self.cloud_job.id,
        })

    def _label(self, row):
        from odoo.addons.incubacloud.controllers._data_load._routes_ops import (
            OpsMixin,
        )
        return OpsMixin._backup_contents_label(row)

    def test_non_prod_with_filestore_label(self):
        row = self._row(self._att().id, True)
        self.assertEqual(self._label(row), 'DB + filestore')

    def test_non_prod_without_filestore_label(self):
        row = self._row(self._att().id, False)
        self.assertEqual(self._label(row), 'DB only')

    def test_prod_no_attachment_label(self):
        row = self._row(False, True)
        self.assertEqual(self._label(row), 'S3 chain')


class TestDuplicityFilenameParser(TransactionCase):
    """The S3 size pass groups bytes per backup-set timestamp using
    duplicity's filename convention — pin it so changes there are
    detected at test time, not in production.
    """

    def test_full_data_file_yields_iso(self):
        from odoo.addons.incubacloud.models.backup_list_executor import (
            _duplicity_filename_to_iso,
        )
        self.assertEqual(
            _duplicity_filename_to_iso(
                'duplicity-full.20260319T020000Z.vol1.difftar.gpg',
            ),
            '20260319T020000Z',
        )

    def test_full_signature_file_yields_iso(self):
        from odoo.addons.incubacloud.models.backup_list_executor import (
            _duplicity_filename_to_iso,
        )
        self.assertEqual(
            _duplicity_filename_to_iso(
                'duplicity-full-signatures.20260319T020000Z.sigtar.gpg',
            ),
            '20260319T020000Z',
        )

    def test_inc_file_yields_to_timestamp(self):
        from odoo.addons.incubacloud.models.backup_list_executor import (
            _duplicity_filename_to_iso,
        )
        self.assertEqual(
            _duplicity_filename_to_iso(
                'duplicity-inc.20260319T020000Z.to.'
                '20260320T020000Z.vol1.difftar.gpg',
            ),
            '20260320T020000Z',
        )

    def test_unrelated_filename_yields_none(self):
        from odoo.addons.incubacloud.models.backup_list_executor import (
            _duplicity_filename_to_iso,
        )
        self.assertIsNone(
            _duplicity_filename_to_iso('some-random.txt'),
        )


# ── _webhook_fields helper ────────────────────────────────────────────────────
#
# Pure-Python serializer that turns ``cloud.job.payload`` trigger metadata into
# the dict the SPA / log terminal consume. Recognises ``webhook`` (a direct
# push) and ``coalesced`` (the follow-up rebuild emitted by
# ``rebuild_instance_executor.on_success`` carrying every push queued during
# the previous rebuild). Anything else collapses to ``{'trigger': ''}`` so the
# UI doesn't paint a half-populated trigger bar for non-webhook jobs.

class TestWebhookFieldsHelper(BaseCase):

    def test_unknown_trigger_collapses_to_empty(self):
        self.assertEqual(_webhook_fields({'trigger': 'manual'}), {'trigger': ''})

    def test_missing_trigger_collapses_to_empty(self):
        self.assertEqual(_webhook_fields({}), {'trigger': ''})

    def test_none_payload_collapses_to_empty(self):
        self.assertEqual(_webhook_fields(None), {'trigger': ''})

    def test_webhook_returns_push_fields_without_coalesced(self):
        out = _webhook_fields({
            'trigger': 'webhook',
            'push_repo': 'owner/repo',
            'push_branch': 'main',
            'push_sha': 'abc1234',
            'push_message': 'fix: x',
            'push_by': 'dev',
        })
        self.assertEqual(out['trigger'], 'webhook')
        self.assertEqual(out['push_sha'], 'abc1234')
        self.assertNotIn('coalesced_pushes', out)

    def test_coalesced_includes_coalesced_pushes_list(self):
        pushes = [
            {'push_sha': 'aaa1111', 'push_message': 'first'},
            {'push_sha': 'bbb2222', 'push_message': 'second'},
        ]
        out = _webhook_fields({
            'trigger': 'coalesced',
            'push_repo': 'owner/repo',
            'push_branch': 'main',
            'push_sha': 'bbb2222',
            'push_message': 'second',
            'push_by': 'dev',
            'coalesced_pushes': pushes,
        })
        self.assertEqual(out['trigger'], 'coalesced')
        self.assertEqual(out['coalesced_pushes'], pushes)

    def test_coalesced_missing_list_defaults_to_empty(self):
        # Defensive: rebuild executor always sets the list, but a malformed
        # payload should still produce a valid dict so the renderer doesn't
        # crash.
        out = _webhook_fields({'trigger': 'coalesced'})
        self.assertEqual(out['coalesced_pushes'], [])

    def test_webhook_fills_missing_push_fields_with_empty_strings(self):
        out = _webhook_fields({'trigger': 'webhook'})
        for k in ('push_repo', 'push_branch', 'push_sha', 'push_message', 'push_by'):
            self.assertEqual(out[k], '')


class TestConnectionRetryAndNotify(TransactionCase):
    """Connection-retry path: opt-in executors retry transient connection
    failures and only raise a ``host_unreachable`` alert once the retries
    are exhausted; non-opt-in executors keep failing fast on the first try.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import asyncssh
        from odoo.addons.incubacloud.models.registry import executor_registry
        from odoo.addons.incubacloud.models.abstract_executor import (
            AbstractSSHExecutor,
        )

        class _ConnLostExecutor(AbstractSSHExecutor):
            """Opt-in executor that always loses its connection."""
            _job_type = None
            _retry_on_connection_loss = True
            _connection_retry_attempts = 3

            def get_commands(self):
                return []

            def run(self):
                raise asyncssh.ConnectionLost('Connection lost')

        class _PlainExecutor(AbstractSSHExecutor):
            """Non-opt-in executor (default fail-fast behaviour)."""
            _job_type = None

            def get_commands(self):
                return []

            def run(self):
                raise asyncssh.ConnectionLost('Connection lost')

        from psycopg2 import OperationalError, errorcodes

        # ``pgcode`` is read-only on a psycopg2 error — only the driver
        # sets it — so a subclass carrying the code is how you build the
        # error PostgreSQL would have raised.
        class _SerializationFailure(OperationalError):
            pgcode = errorcodes.SERIALIZATION_FAILURE

        class _ConnectionFailure(OperationalError):
            pgcode = '08006'  # connection_failure — not a retry code

        class _SerializationExecutor(AbstractSSHExecutor):
            """Loses a row race the way two crons on one row do."""
            _job_type = None

            def get_commands(self):
                return []

            def run(self):
                raise _SerializationFailure('could not serialize access')

        class _PgFailureExecutor(AbstractSSHExecutor):
            """An OperationalError that is nobody's transient race."""
            _job_type = None

            def get_commands(self):
                return []

            def run(self):
                raise _ConnectionFailure('connection to server was lost')

        executor_registry._executors['test_conn_retry'] = _ConnLostExecutor
        executor_registry._executors['test_conn_plain'] = _PlainExecutor
        executor_registry._executors['test_serialization'] = (
            _SerializationExecutor
        )
        executor_registry._executors['test_pg_failure'] = _PgFailureExecutor
        for code in (
            'test_conn_retry', 'test_conn_plain',
            'test_serialization', 'test_pg_failure',
        ):
            cls.addClassCleanup(
                executor_registry._executors.pop, code, None)

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'conn-retry-host',
            'ip_address': '10.0.0.9',
            'user': 'root',
            'login_type': 'password',
            'password': 'x',
            'wildcard_domain': 'conn-retry.test.local',
        })
        _ensure_job_type(self.env, 'host_metrics', apply_to='host')
        _ensure_job_type(self.env, 'test_conn_retry', apply_to='host')
        _ensure_job_type(self.env, 'test_conn_plain', apply_to='host')
        _ensure_job_type(self.env, 'test_serialization', apply_to='host')
        _ensure_job_type(self.env, 'test_pg_failure', apply_to='host')

    def _enqueue(self, code):
        jid = self.env['cloud.job'].enqueue(self.host.id, False, code)
        return self.env['cloud.job'].browse(jid)

    def test_opt_in_job_type_enqueues_with_ten_retries(self):
        # Real wiring: the production host_metrics executor opts in.
        # Ten attempts ~30 s apart is the ~10 min tolerance that keeps a
        # host's own maintenance reboot from paging anyone.
        job = self._enqueue('host_metrics')
        self.assertEqual(job.queue_job_id.max_retries, 10)

    def test_plain_job_type_enqueues_with_single_retry(self):
        job = self._enqueue('test_conn_plain')
        self.assertEqual(job.queue_job_id.max_retries, 1)

    def test_transient_error_always_asks_for_a_retry(self):
        """Whether a retry is the last one is queue_job's decision.

        ``execute`` used to work it out from the retry counter and
        alert on what it judged to be the final attempt. It judged
        wrong: the runner can hand a job one more attempt than the
        budget, so the alert fired on jobs that then connected and
        finished ``done``. Now every transient failure asks for a
        retry, and ``Job.perform`` converts the last one into a
        ``FailedJobError`` by itself.
        """
        from odoo.addons.queue_job.exception import RetryableJobError
        job = self._enqueue('test_conn_retry')
        # Fresh job → queue_job_id.retry == 0 → this is attempt 1.
        with self.assertRaises(RetryableJobError):
            job.execute()

    def test_transient_error_on_last_attempt_still_asks_for_a_retry(self):
        """Even at the end of the budget, execute() does not decide.

        The counter says this is the final attempt, but the answer is
        the same ``RetryableJobError``: it is ``Job.perform`` that
        compares ``retry`` against ``max_retries``, and only its verdict
        is trustworthy enough to alert on.
        """
        from odoo.addons.queue_job.exception import RetryableJobError
        job = self._enqueue('test_conn_retry')
        self.env.cr.execute(
            "UPDATE queue_job SET retry = 2 WHERE id = %s",
            (job.queue_job_id.id,),
        )
        job.queue_job_id.invalidate_recordset(['retry'])
        with self.assertRaises(RetryableJobError) as caught:
            job.execute()
        # The message carries the marker the terminal-state hook keys on.
        from odoo.addons.incubacloud.models.abstract_executor import (
            CONNECTION_RETRY_MARKER,
        )
        self.assertIn(CONNECTION_RETRY_MARKER, str(caught.exception))

    def test_serialization_failure_is_left_to_queue_job(self):
        """A lost row race must not become a permanently failed job.

        queue_job already postpones and retries serialisation failures.
        Wrapping one in ``JobError`` made it terminal *and* printed a
        traceback that the tenant log scraper reported back as an
        ``instance_error_logs`` alert — an outage report for two crons
        stamping the same row.
        """
        from psycopg2 import OperationalError
        job = self._enqueue('test_serialization')
        with self.assertRaises(OperationalError):
            job.execute()

    def test_other_operational_error_still_fails_the_job(self):
        """Only the concurrency codes are handed back to queue_job."""
        from odoo.addons.queue_job.exception import JobError
        job = self._enqueue('test_pg_failure')
        with self.assertRaises(JobError):
            job.execute()

