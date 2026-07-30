"""Tests for cloud.project.unlink guard and DeleteProjectExecutor."""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestCloudProjectUnlinkGuard(TransactionCase):
    """A project can only be deleted once none of its instances are still
    on a host — a deployed instance must be removed from its host first,
    or the project delete would orphan the remote stack."""

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({
            'name': 'cleanup-proj',
        })
        self.host_a = self.env['cloud.host'].create({
            'name': 'host-a', 'ip_address': '10.0.0.1',
            'port': 22, 'user': 'root', 'login_type': 'ssh_key',
            'wildcard_domain': 'a.example.com',
        })

    def _instance(self, environment, state='draft'):
        return self.env['cloud.instance'].create({
            'name': f'inst-{environment}-{state}',
            'project_id': self.project.id,
            'environment': environment,
            'host_id': self.host_a.id,
            'state': state,
        })

    def test_unlink_without_instances_is_allowed(self):
        self.project.unlink()
        self.assertFalse(self.project.exists())

    def test_unlink_with_only_draft_instances_is_allowed(self):
        # Draft instances carry no remote footprint, so they cascade
        # cleanly with the project.
        self._instance('staging', state='draft')
        self.project.unlink()
        self.assertFalse(self.project.exists())

    def test_unlink_blocked_by_a_deployed_instance(self):
        inst = self._instance('production', state='deployed')
        with self.assertRaises(UserError) as caught:
            self.project.unlink()
        self.assertIn(inst.name, str(caught.exception))
        self.assertTrue(self.project.exists())

    def test_unlink_no_longer_enqueues_remote_cleanup(self):
        # The old dead cleanup block is gone: deleting a project with
        # draft instances enqueues nothing (their host dirs, if any, are
        # already cleaned by the per-instance teardown).
        self._instance('staging', state='draft')
        with patch.object(
            type(self.env['cloud.job']), 'enqueue',
        ) as mock_enqueue:
            self.project.unlink()
            mock_enqueue.assert_not_called()


class TestDeleteProjectExecutorRemoteFolder(TransactionCase):
    """_remote_folder() must reject anything that could escape ~/."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.incubacloud.models.delete_project_executor import (
            DeleteProjectExecutor,
        )
        cls.cls = DeleteProjectExecutor

    def _executor(self, payload):
        ex = self.cls.__new__(self.cls)
        job = MagicMock()
        job.payload = payload
        ex.job = job
        return ex

    def test_valid_folder(self):
        self.assertEqual(
            self._executor({'remote_folder': 'cleanup-proj'})
            ._remote_folder(),
            'cleanup-proj',
        )

    def test_strips_whitespace(self):
        self.assertEqual(
            self._executor({'remote_folder': '  foo  '})
            ._remote_folder(),
            'foo',
        )

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            self._executor({})._remote_folder()
        with self.assertRaises(ValueError):
            self._executor({'remote_folder': ''})._remote_folder()

    def test_rejects_special_values(self):
        for bad in ('/', '~', '.', '..'):
            with self.assertRaises(ValueError):
                self._executor({'remote_folder': bad})._remote_folder()

    def test_rejects_slash(self):
        with self.assertRaises(ValueError):
            self._executor({'remote_folder': 'a/b'})._remote_folder()
        with self.assertRaises(ValueError):
            self._executor({'remote_folder': '/absolute'})._remote_folder()

    def test_rejects_hidden(self):
        with self.assertRaises(ValueError):
            self._executor({'remote_folder': '.hidden'})._remote_folder()

    def test_job_type_binding(self):
        self.assertEqual(self.cls._job_type, 'delete_project')
