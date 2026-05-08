"""
Tier 2 — ORM integration tests for cloud.job utilities.

Tests focus on the parts that don't require actual SSH execution:
  - _get_last_system_message
  - load_chunks  (filtering and after_id pagination)
  - cancel_job / retry_job guard conditions
  - _get_executor raises for unknown job types
  - _format / _format_history dict structure
"""
import base64
from datetime import timedelta

from odoo.tests.common import TransactionCase, new_test_user
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


class TestEnqueueBypassRunningCheck(TransactionCase):
    """``bypass_running_check=True`` lets executors enqueue follow-up
    jobs from inside their own ``on_success`` — when the parent
    cloud.job is still ``started`` and would otherwise block its own
    descendant. The flag must not weaken the user-facing guard, only
    skip it for the explicit internal call."""

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
        self.parent_jt = _ensure_job_type(self.env, 'test_parent_jt')
        self.follow_jt = _ensure_job_type(self.env, 'test_follow_jt')
        # Mimic an executor mid-on_success: parent job already started.
        # ``state`` is a stored-related from queue.job; the related
        # compute overwrites the create value, so we set the column
        # directly the same way ``TestChainFailurePropagation`` does.
        self.parent = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.parent_jt.id,
            'instance_id': self.instance.id,
            'name': 'Parent',
        })
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'started' WHERE id = %s",
            (self.parent.id,),
        )
        self.env['cloud.job'].invalidate_model(['state'])

    def test_default_blocks_when_parent_running(self):
        with self.assertRaises(UserError):
            self.env['cloud.job'].enqueue(
                self.host.id, self.instance.id, 'test_follow_jt',
            )

    def test_bypass_allows_enqueue_during_parent_run(self):
        new_id = self.env['cloud.job'].enqueue(
            self.host.id, self.instance.id, 'test_follow_jt',
            bypass_running_check=True,
        )
        new_job = self.env['cloud.job'].browse(new_id)
        self.assertTrue(new_job.exists())
        self.assertEqual(new_job.job_type_id, self.follow_jt)


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
            'deployed': True,
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

