"""Tests for cloud.instance.move_to_host orchestration.

The move reuses existing executors in one two-host ``enqueue_chain``; the
source is only torn down as the last step, so a failure anywhere earlier
breaks the chain and leaves the source recoverable. These tests pin the
chain shape, the validations, the watchdog recovery, and the rollback —
the SSH executors themselves are mocked away.

Updated 2026-07 to cover the robust rollback that cancels the chain and
enqueues cleanup+start instead of silently ignoring the in-flight jobs.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestInstanceMove(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'move-proj'})
        self.source = self.env['cloud.host'].create({
            'name': 'src', 'ip_address': '10.0.0.1', 'port': 22,
            'user': 'root', 'login_type': 'ssh_key',
            'wildcard_domain': 'src.example.com',
            'status': 'compatible', 'traefik_deployed': True,
        })
        self.target = self.env['cloud.host'].create({
            'name': 'tgt', 'ip_address': '10.0.0.2', 'port': 22,
            'user': 'root', 'login_type': 'ssh_key',
            'wildcard_domain': 'tgt.example.com',
            'status': 'compatible', 'traefik_deployed': True,
        })
        self.bb = self.env['cloud.backup.backend'].create({
            'name': 'move-bb',
            'backend_type': 's3',
            's3_bucket': 'move-bucket',
        })
        self.inst = self.env['cloud.instance'].create({
            'name': 'movable', 'project_id': self.project.id,
            'environment': 'production', 'host_id': self.source.id,
            'deployed': True, 'backup_backend_id': self.bb.id,
        })

    def _patch_chain(self):
        return patch.object(
            type(self.env['cloud.job']), 'enqueue_chain',
            return_value=[1, 2, 3, 4, 5, 6, 7],
        )

    def _patch_enqueue(self, return_value=8):
        return patch.object(
            type(self.env['cloud.job']), 'enqueue',
            return_value=return_value,
        )

    # ── Chain shape & validations ──────────────────────────────────────

    def test_move_builds_two_host_chain(self):
        with self._patch_chain() as m:
            res = self.inst.move_to_host(self.target)
        self.assertTrue(res['ok'])
        steps = m.call_args[0][0]
        expected_codes = [
            'deploy_instance', 'stop_instance', 'backup_create',
            'backup_download', 'restore_instance', 'move_cutover',
            'move_cleanup_source',
        ]
        self.assertEqual(
            [s['job_type_code'] for s in steps], expected_codes,
        )
        self.assertEqual(steps[0]['host_id'], self.target.id)
        self.assertEqual(steps[1]['host_id'], self.source.id)
        self.assertEqual(steps[2]['host_id'], self.source.id)
        self.assertEqual(steps[3]['host_id'], self.source.id)
        self.assertEqual(steps[4]['host_id'], self.target.id)
        self.assertEqual(steps[5]['host_id'], self.target.id)
        self.assertEqual(steps[6]['host_id'], self.source.id)
        self.assertEqual(steps[1]['payload'], {'services': ['odoo']})
        self.assertEqual(
            steps[4]['payload']['source_job_id'], '__chain_job_3__',
        )
        idx_stop = expected_codes.index('stop_instance')
        idx_create = expected_codes.index('backup_create')
        idx_download = expected_codes.index('backup_download')
        self.assertLess(idx_create, idx_download)
        self.assertLess(idx_stop, idx_download)
        self.assertEqual(steps[idx_stop]['payload']['services'], ['odoo'])
        self.assertEqual(self.inst.move_origin_host_id, self.source)
        self.assertEqual(self.inst.move_target_host_id, self.target)
        self.assertEqual(
            self.inst.move_chain_job_ids, '1,2,3,4,5,6,7',
        )

    def test_move_validations(self):
        with self._patch_chain():
            with self.assertRaises(UserError):
                self.inst.move_to_host(self.source)
            self.inst.deployed = False
            with self.assertRaises(UserError):
                self.inst.move_to_host(self.target)
            self.inst.deployed = True
            self.target.traefik_deployed = False
            with self.assertRaises(UserError):
                self.inst.move_to_host(self.target)

    def test_move_blocked_when_already_moving(self):
        self.inst.move_origin_host_id = self.source.id
        with self._patch_chain():
            with self.assertRaises(UserError):
                self.inst.move_to_host(self.target)

    def test_move_requires_backup_backend(self):
        self.inst.backup_backend_id = False
        self.env['ir.config_parameter'].sudo().set_param(
            'incubacloud.backup_backend_id', '',
        )
        with self._patch_chain():
            with self.assertRaises(UserError):
                self.inst.move_to_host(self.target)

    def test_core_deploy_job_type(self):
        self.assertEqual(
            self.inst._move_deploy_job_type(), 'deploy_instance',
        )

    # ── Rollback — new robust behaviour ────────────────────────────────

    def test_rollback_enqueues_cleanup_and_start(self):
        """Rollback cancels the chain and enqueues two recovery jobs."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'move_target_host_id': self.target.id,
            'move_chain_job_ids': '1,2,3',
        })
        with self._patch_enqueue(return_value=42) as m:
            res = self.inst.rollback_move()
        self.assertTrue(res['ok'])
        self.assertEqual(res['job_id'], 42)
        # Two enqueues: start_instance on source, cleanup on target.
        self.assertEqual(m.call_count, 2)
        calls = m.call_args_list
        # First call: start_instance on source
        self.assertEqual(calls[0][0][0], self.source.id)
        self.assertEqual(calls[0][0][2], 'start_instance')
        self.assertTrue(calls[0][1].get('bypass_running_check'))
        # Second call: move_rollback_cleanup on target
        self.assertEqual(calls[1][0][0], self.target.id)
        self.assertEqual(calls[1][0][2], 'move_rollback_cleanup')
        self.assertTrue(calls[1][1].get('bypass_running_check'))
        # Markers are NOT cleared — cleanup does that on success.
        self.assertEqual(self.inst.move_origin_host_id, self.source)
        self.assertEqual(self.inst.move_target_host_id, self.target)
        self.assertTrue(self.inst.move_rollback_in_progress)

    def test_rollback_requires_in_progress(self):
        with self.assertRaises(UserError):
            self.inst.rollback_move()

    def test_rollback_rejects_when_cutover_completed(self):
        """Rollback raises when host_id already flipped (cutover done)."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'host_id': self.target.id,
        })
        with self.assertRaises(UserError) as ctx:
            self.inst.rollback_move()
        self.assertIn('already completed', str(ctx.exception))

    def test_rollback_rejects_duplicate(self):
        """Second rollback call while one is running raises."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'move_target_host_id': self.target.id,
            'move_rollback_in_progress': True,
        })
        with self.assertRaises(UserError) as ctx:
            self.inst.rollback_move()
        self.assertIn('already in progress', str(ctx.exception))

    def test_do_rollback_move_host_mismatch_returns_false(self):
        """_do_rollback_move returns False when host_id ≠ origin."""
        self.inst.move_origin_host_id = self.source.id
        self.inst.host_id = self.target.id
        result = self.inst._do_rollback_move()
        self.assertFalse(result)

    def test_do_rollback_move_no_origin_returns_false(self):
        """_do_rollback_move returns False when no marker is set."""
        result = self.inst._do_rollback_move()
        self.assertFalse(result)

    # ── Chain cancellation ─────────────────────────────────────────────

    def test_cancel_move_chain_with_no_chain_jobs(self):
        """_cancel_move_chain is a no-op when move_chain_job_ids is empty."""
        self.inst.move_chain_job_ids = False
        with self._patch_enqueue() as m:
            self.inst._cancel_move_chain()
        m.assert_not_called()

    def test_cancel_move_chain_skips_terminal_jobs(self):
        """Only active jobs are cancelled; terminal ones are skipped."""
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'deploy_instance')], limit=1,
        )
        jobs = []
        for state in ('done', 'failed', 'cancelled'):
            job = self.env['cloud.job'].create({
                'host_id': self.source.id,
                'instance_id': self.inst.id,
                'job_type_id': job_type.id,
                'name': f'Job {state}',
            })
            self.env.cr.execute(
                "UPDATE cloud_job SET state = %s WHERE id = %s",
                (state, job.id),
            )
            jobs.append(job)
        self.env['cloud.job'].invalidate_model(['state'])
        self.inst.move_chain_job_ids = ','.join(
            str(j.id) for j in jobs
        )
        try:
            self.inst._cancel_move_chain()
        except Exception:
            self.fail("_cancel_move_chain should not raise on terminal jobs")

    def test_cancel_move_chain_cancels_active_jobs(self):
        """Active jobs in the chain get cancelled."""
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'deploy_instance')], limit=1,
        )
        jobs = [
            self.env['cloud.job'].create({
                'host_id': self.source.id,
                'instance_id': self.inst.id,
                'job_type_id': job_type.id,
                'name': f'Active job {i}',
            })
            for i in range(2)
        ]
        for job in jobs:
            self.env.cr.execute(
                "UPDATE cloud_job SET state = %s WHERE id = %s",
                ('started', job.id),
            )
        self.env['cloud.job'].invalidate_model(['state'])
        self.inst.move_chain_job_ids = ','.join(
            str(j.id) for j in jobs
        )
        with patch.object(
            type(self.env['cloud.job']), 'cancel_job',
        ) as mock_cancel:
            self.inst._cancel_move_chain()
        self.assertEqual(mock_cancel.call_count, 2)

    def test_get_move_chain_jobs_preserves_order(self):
        """_get_move_chain_jobs returns jobs in chain order, not DB order."""
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'deploy_instance')], limit=1,
        )
        jobs_created = [
            self.env['cloud.job'].create({
                'host_id': self.source.id,
                'instance_id': self.inst.id,
                'job_type_id': job_type.id,
                'name': f'Job {i}',
            })
            for i in range(3)
        ]
        ids = [j.id for j in jobs_created]
        self.inst.move_chain_job_ids = ','.join(
            str(ids[i]) for i in (1, 0, 2)  # middle, first, last
        )
        jobs = self.inst._get_move_chain_jobs()
        self.assertEqual([j.id for j in jobs], [ids[1], ids[0], ids[2]])

    # ── Watchdog recovery (cron_recover_stuck_moves) ────────────────────

    def _make_terminal_job(self, state='failed'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', 'deploy_instance')], limit=1,
        )
        return self.env['cloud.job'].create({
            'host_id': self.source.id,
            'instance_id': self.inst.id,
            'job_type_id': jt.id,
            'name': 'Terminal move job',
            'state': state,
        })

    def _make_active_job(self, state='started'):
        """Create a ``cloud.job`` and force-persist its state via SQL."""
        jt = self.env['cloud.job.type'].search(
            [('code', '=', 'deploy_instance')], limit=1,
        )
        job = self.env['cloud.job'].create({
            'host_id': self.source.id,
            'instance_id': self.inst.id,
            'job_type_id': jt.id,
            'name': 'Active move job',
        })
        self.env.cr.execute(
            "UPDATE cloud_job SET state = %s WHERE id = %s",
            (state, job.id),
        )
        self.env['cloud.job'].invalidate_model(['state'])
        return job

    def test_watchdog_triggers_rollback_and_alerts(self):
        """When all jobs are terminal, watchdog calls _do_rollback_move
        (enqueues cleanup+start) and creates a move_stuck alert. Markers
        are NOT cleared — the rollback cleanup clears them on success."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'move_target_host_id': self.target.id,
            'host_id': self.source.id,
        })
        self._make_terminal_job('failed')
        with self._patch_enqueue() as m:
            self.env['cloud.instance'].cron_recover_stuck_moves()
        # Two enqueue calls: start_instance + move_rollback_cleanup.
        self.assertEqual(m.call_count, 2)
        # Markers are NOT cleared (rollback cleanup does that on success).
        self.assertTrue(self.inst.move_origin_host_id)
        self.assertTrue(self.inst.move_rollback_in_progress)
        # Alert created.
        alert = self.env['cloud.alert'].sudo().search([
            ('instance_id', '=', self.inst.id),
            ('code', '=', 'move_stuck'),
            ('state', '=', 'active'),
        ])
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'critical')

    def test_watchdog_skips_move_in_progress(self):
        """When an active job exists, watchdog leaves the instance alone."""
        self.inst.move_origin_host_id = self.source.id
        self._make_terminal_job('failed')
        self._make_active_job('started')
        with patch.object(
            type(self.env['cloud.job']), 'enqueue',
        ) as m:
            self.env['cloud.instance'].cron_recover_stuck_moves()
        m.assert_not_called()
        self.assertEqual(self.inst.move_origin_host_id, self.source)
        self.assertEqual(
            self.env['cloud.alert'].sudo().search_count([
                ('instance_id', '=', self.inst.id),
                ('code', '=', 'move_stuck'),
            ]),
            0,
        )

    def test_watchdog_skips_hidden_jobs(self):
        """Hidden job types don't count as in-flight."""
        self.inst.move_origin_host_id = self.source.id
        jt = self.env['cloud.job.type'].search(
            [('code', '=', 'host_metrics')], limit=1,
        )
        job = self.env['cloud.job'].create({
            'host_id': self.source.id,
            'instance_id': self.inst.id,
            'job_type_id': jt.id,
            'name': 'Metrics poll',
        })
        self.env.cr.execute(
            "UPDATE cloud_job SET state = %s WHERE id = %s",
            ('started', job.id),
        )
        self.env['cloud.job'].invalidate_model(['state'])
        with self._patch_enqueue() as m:
            self.env['cloud.instance'].cron_recover_stuck_moves()
        m.assert_called()

    def test_watchdog_dedup_alert(self):
        """Second pass with existing active move_stuck alert does not
        create a duplicate."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'host_id': self.source.id,
        })
        self._make_terminal_job('failed')
        self.env['cloud.alert'].sudo().create({
            'code': 'move_stuck',
            'level': 'critical',
            'message': 'Existing alert',
            'instance_id': self.inst.id,
            'host_id': self.source.id,
        })
        with self._patch_enqueue():
            self.env['cloud.instance'].cron_recover_stuck_moves()
        self.assertEqual(
            self.env['cloud.alert'].sudo().search_count([
                ('instance_id', '=', self.inst.id),
                ('code', '=', 'move_stuck'),
            ]),
            1,
        )

    # ── Cron: failed rollback cleanup ──────────────────────────────────

    def test_cron_recovers_failed_rollback_cleanup(self):
        """When rollback was started but cleanup failed (no active jobs,
        markers still set), the cron clears markers and raises a
        move_rollback_failed alert."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'move_target_host_id': self.target.id,
            'move_chain_job_ids': '1,2,3',
            'move_rollback_in_progress': True,
            'host_id': self.source.id,
        })
        with self._patch_enqueue():
            self.env['cloud.instance'].cron_recover_stuck_moves()
        # Markers cleared by cron.
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertFalse(self.inst.move_target_host_id)
        self.assertFalse(self.inst.move_chain_job_ids)
        self.assertFalse(self.inst.move_rollback_in_progress)
        # move_rollback_failed alert created.
        alert = self.env['cloud.alert'].sudo().search([
            ('instance_id', '=', self.inst.id),
            ('code', '=', 'move_rollback_failed'),
            ('state', '=', 'active'),
        ])
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'warning')

    def test_cron_skips_rollback_in_progress_with_active_jobs(self):
        """When rollback is in progress AND there are active jobs,
        the cron leaves everything alone."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'move_target_host_id': self.target.id,
            'move_rollback_in_progress': True,
            'host_id': self.source.id,
        })
        self._make_active_job('started')
        with self._patch_enqueue() as m:
            self.env['cloud.instance'].cron_recover_stuck_moves()
        m.assert_not_called()
        self.assertTrue(self.inst.move_origin_host_id)
        self.assertTrue(self.inst.move_rollback_in_progress)
        self.assertEqual(
            self.env['cloud.alert'].sudo().search_count([
                ('instance_id', '=', self.inst.id),
                ('code', '=', 'move_rollback_failed'),
            ]),
            0,
        )

    def test_cron_move_stuck_alert_only_when_no_rollback_in_progress(self):
        """move_stuck alert is only created when the cron triggers
        recovery (move_rollback_in_progress was False)."""
        self.inst.write({
            'move_origin_host_id': self.source.id,
            'host_id': self.source.id,
        })
        self._make_terminal_job('failed')
        with self._patch_enqueue():
            self.env['cloud.instance'].cron_recover_stuck_moves()
        self.assertEqual(
            self.env['cloud.alert'].sudo().search_count([
                ('instance_id', '=', self.inst.id),
                ('code', '=', 'move_stuck'),
                ('state', '=', 'active'),
            ]),
            1,
        )
