"""Tests for cloud.project.unlink cleanup and DeleteProjectExecutor."""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase


class TestCloudProjectUnlinkEnqueuesCleanup(TransactionCase):
    """unlink() must schedule delete_project jobs on every host where the
    project had instances, carrying the remote_folder in the payload."""

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
        self.host_b = self.env['cloud.host'].create({
            'name': 'host-b', 'ip_address': '10.0.0.2',
            'port': 22, 'user': 'root', 'login_type': 'ssh_key',
            'wildcard_domain': 'b.example.com',
        })

    def _instance(self, host, environment):
        return self.env['cloud.instance'].create({
            'name': f'inst-{host.name}-{environment}',
            'project_id': self.project.id,
            'environment': environment,
            'host_id': host.id,
        })

    def test_unlink_without_instances_enqueues_nothing(self):
        with patch.object(
            type(self.env['cloud.job']), 'enqueue',
        ) as mock_enqueue:
            self.project.unlink()
            mock_enqueue.assert_not_called()

    def test_unlink_enqueues_delete_project_per_host(self):
        self._instance(self.host_a, 'production')
        self._instance(self.host_b, 'staging')
        folder = self.project.remote_folder
        self.assertTrue(folder)
        expected_hosts = {self.host_a.id, self.host_b.id}

        with patch.object(
            type(self.env['cloud.job']), 'enqueue',
            return_value=self.env['cloud.job'],
        ) as mock_enqueue:
            self.project.unlink()

        self.assertEqual(mock_enqueue.call_count, 2)
        seen_hosts = set()
        for call in mock_enqueue.call_args_list:
            args, kwargs = call
            host_id, instance_id, code = args[0], args[1], args[2]
            self.assertEqual(code, 'delete_project')
            self.assertFalse(instance_id)
            self.assertEqual(
                kwargs.get('payload'), {'remote_folder': folder},
            )
            seen_hosts.add(host_id)
        self.assertEqual(seen_hosts, expected_hosts)


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
