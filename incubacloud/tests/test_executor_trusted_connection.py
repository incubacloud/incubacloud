"""The executor is a trusted reader of the host connection material.

Jobs run under the env of whoever enqueued them (queue_job restores the
caller's env), while ``ip_address``/``port``/``user``/``password``/
``key_file`` are developer-gated at the ORM layer. Without an explicit
elevation, a consultant-triggered deploy would raise ``AccessError``
inside the worker — which is exactly what happened before SEC-009b.

The elevation must stay confined to the executor path: the public
``ssh_connect_kwargs()`` returns the password and must keep refusing a
low-privilege caller.
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import asyncssh

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user


class TrustedConnectionCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Optional modules may add NOT NULL columns to res_partner without
        # defaults; new_test_user() creates a partner without those fields.
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
        self.consultant = new_test_user(
            self.env, login='tc_consultant',
            groups='base.group_user,incubacloud.group_cloud_consultant',
        )
        self.host = self.env['cloud.host'].create({
            'name': 'trusted-host',
            'ip_address': '10.0.0.9',
            'port': 2222,
            'user': 'deploy',
            'login_type': 'password',
            'password': 'sekret',
            'wildcard_domain': 'trusted.example.com',
            'known_hosts_key': '[10.0.0.9]:2222 ssh-ed25519 AAAAC3Nz',
        })
        self.project = self.env['cloud.project'].create({
            'name': 'trusted-proj',
            'member_ids': [(4, self.consultant.id)],
        })
        self.instance = self.env['cloud.instance'].create({
            'name': 'trusted-inst',
            'project_id': self.project.id,
            'environment': 'staging',
            'host_id': self.host.id,
        })
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'deploy_instance')], limit=1,
        )
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': job_type.id,
            'name': 'Deploy Instance',
        })

    def _executor_as(self, user):
        """Build the executor the way ``cloud.job.execute()`` does."""
        from odoo.addons.incubacloud.models.start_instance_executor import (
            StartInstanceExecutor,
        )
        job = self.job.with_user(user)
        return StartInstanceExecutor(
            job_record=job, host_record=job.host_id,
        )

    # -- the executor may read the endpoint ------------------------------

    def test_consultant_executor_reads_the_connection_triplet(self):
        executor = self._executor_as(self.consultant)
        self.assertEqual(executor.host, '10.0.0.9')
        self.assertEqual(executor.port, 2222)
        self.assertEqual(executor.username, 'deploy')

    def test_ansible_inventory_reads_the_connection_triplet(self):
        from odoo.addons.incubacloud.models.ansible_executor import (
            AnsibleExecutor,
        )
        executor = AnsibleExecutor.__new__(AnsibleExecutor)
        executor._host_record = self.job.with_user(self.consultant).host_id
        inventory = executor.build_inventory(None, '/tmp/known_hosts')
        hostvars = inventory['all']['hosts'][executor.inventory_hostname()]
        self.assertEqual(hostvars['ansible_host'], '10.0.0.9')
        self.assertEqual(hostvars['ansible_port'], 2222)
        self.assertEqual(hostvars['ansible_user'], 'deploy')

    def test_get_transport_connects_as_a_consultant(self):
        """The whole transport path must work under the enqueuing user."""
        conn = MagicMock(spec=asyncssh.SSHClientConnection)

        @asynccontextmanager
        async def _fake_connect(**kwargs):
            _fake_connect.kwargs = kwargs
            yield conn

        host = self.host.with_user(self.consultant)

        async def _run():
            with patch.object(asyncssh, 'connect', _fake_connect), \
                    patch.object(
                        asyncssh, 'import_known_hosts',
                        MagicMock(return_value=None),
                    ):
                async with host.get_transport():
                    pass

        asyncio.run(_run())
        self.assertEqual(_fake_connect.kwargs['host'], '10.0.0.9')
        self.assertEqual(_fake_connect.kwargs['port'], 2222)
        self.assertEqual(_fake_connect.kwargs['username'], 'deploy')

    # -- but the public method stays closed ------------------------------

    def test_ssh_connect_kwargs_still_refuses_a_consultant(self):
        # It returns the password, so the elevation must NOT live in it.
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).ssh_connect_kwargs()

    def test_consultant_still_cannot_read_the_fields_directly(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).read(['ip_address'])
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).read(['password'])
