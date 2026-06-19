"""Tests for cloud.instance.move_to_host orchestration.

The move reuses existing executors in one two-host ``enqueue_chain``; the
source is only torn down as the last step, so a failure anywhere earlier
breaks the chain and leaves the source recoverable. These tests pin the
chain shape, the validations, and the rollback — the SSH executors
themselves are mocked away.
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
        self.inst = self.env['cloud.instance'].create({
            'name': 'movable', 'project_id': self.project.id,
            'environment': 'production', 'host_id': self.source.id,
            'deployed': True,
        })

    def _patch_chain(self):
        return patch.object(
            type(self.env['cloud.job']), 'enqueue_chain',
            return_value=[1, 2, 3, 4, 5, 6],
        )

    def test_move_builds_two_host_chain(self):
        """deploy/restore/cutover land on the target; stop/download/cleanup
        on the source; restore references the download job; the in-flight
        marker is stamped with the source host."""
        with self._patch_chain() as m:
            res = self.inst.move_to_host(self.target)
        self.assertTrue(res['ok'])
        steps = m.call_args[0][0]
        self.assertEqual(
            [s['job_type_code'] for s in steps],
            ['deploy_instance', 'stop_instance', 'backup_download',
             'restore_instance', 'move_cutover', 'move_cleanup_source'],
        )
        self.assertEqual(steps[0]['host_id'], self.target.id)
        self.assertEqual(steps[1]['host_id'], self.source.id)
        self.assertEqual(steps[2]['host_id'], self.source.id)
        self.assertEqual(steps[3]['host_id'], self.target.id)
        self.assertEqual(steps[4]['host_id'], self.target.id)
        self.assertEqual(steps[5]['host_id'], self.source.id)
        self.assertEqual(
            steps[3]['payload']['source_job_id'], '__chain_job_2__',
        )
        self.assertEqual(self.inst.move_origin_host_id, self.source)

    def test_move_validations(self):
        """Same host, undeployed, and not-ready target all raise."""
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
        """A second move is refused while one is in flight."""
        self.inst.move_origin_host_id = self.source.id
        with self._patch_chain():
            with self.assertRaises(UserError):
                self.inst.move_to_host(self.target)

    def test_rollback_clears_marker_and_starts_source(self):
        """Rollback clears the marker and brings the source back up."""
        self.inst.move_origin_host_id = self.source.id
        with patch.object(
            type(self.env['cloud.job']), 'enqueue', return_value=7,
        ) as m:
            res = self.inst.rollback_move()
        self.assertTrue(res['ok'])
        self.assertFalse(self.inst.move_origin_host_id)
        args, kwargs = m.call_args
        self.assertEqual(args[0], self.source.id)
        self.assertEqual(args[2], 'start_instance')
        self.assertTrue(kwargs.get('bypass_running_check'))

    def test_rollback_requires_in_progress(self):
        """Rollback with no move in flight raises."""
        with self.assertRaises(UserError):
            self.inst.rollback_move()

    def test_core_deploy_job_type(self):
        """A non-tenant instance uses the bare core deploy."""
        self.assertEqual(
            self.inst._move_deploy_job_type(), 'deploy_instance',
        )
