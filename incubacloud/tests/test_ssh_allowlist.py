"""Who may open SSH on a host, changed live from the panel.

A panel moved to another host could not reach its customer's servers:
their firewalls admitted the old host only, and nothing could change that
short of a new hardening run (18-oct-2026). ``cloud.host
._ssh_allowlist_change`` runs ``ssh_allowlist.sh`` on the host (whose own
behaviour ``tests/shell/ssh_allowlist.bats`` covers) and keeps the list the
host then allows in ``allowed_ssh_ips``, which the next hardening rebuilds
the list from.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport


class TestTheSshAllowlistChangesLive(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'al-host', 'ip_address': '192.0.2.60', 'user': 'ubuntu',
            'wildcard_domain': 'al.example.com',
            'allowed_ssh_ips': '203.0.113.7',
        })
        self.host.hardened = True

    def _change(self, op, ips, answer='IC_SSH_ALLOW 203.0.113.7, 77.42.42.201',
                status=0):
        transport = MagicMock(spec=SSHTransport)
        script = CommandResult(f'[incubacloud] ok\n{answer}', status)
        transport.run = AsyncMock(side_effect=lambda cmd, **kw: (
            script if 'ssh_allowlist.sh' in cmd else CommandResult('', 0)
        ))
        transport.upload_text_files = AsyncMock()

        @asynccontextmanager
        async def _open(host):
            yield transport

        with patch.object(type(self.host), 'get_transport', _open):
            result = self.host._ssh_allowlist_change(op, ips)
        return result, transport

    def test_an_address_is_let_in_and_remembered(self):
        result, transport = self._change('add', ['77.42.42.201'])
        self.assertEqual(result, '203.0.113.7, 77.42.42.201')
        self.assertEqual(
            self.host.allowed_ssh_ips, '203.0.113.7, 77.42.42.201',
        )
        files = transport.upload_text_files.await_args.args[0]
        self.assertEqual(
            sorted(path.split('/', 3)[3] for path in files),
            ['lib/common.sh', 'ssh_allowlist.sh'],
        )
        commands = [c.args[0] for c in transport.run.await_args_list]
        run = next(c for c in commands if 'ssh_allowlist.sh' in c)
        self.assertIn(' add 77.42.42.201', run)
        self.assertTrue(commands[-1].startswith('rm -rf /tmp/.incubacloud-'))

    def test_an_address_taken_out_is_forgotten_too(self):
        self.host.allowed_ssh_ips = '203.0.113.7, 77.42.42.201'
        self._change('remove', ['77.42.42.201'],
                     answer='IC_SSH_ALLOW 203.0.113.7')
        self.assertEqual(self.host.allowed_ssh_ips, '203.0.113.7')

    def test_the_field_keeps_what_the_host_allows_not_what_it_held(self):
        # The field lacked the control address the hardening run added:
        # the host's own list is what a later run must rebuild.
        self._change('add', ['77.42.42.201'],
                     answer='IC_SSH_ALLOW 198.51.100.9, 203.0.113.7, 77.42.42.201')
        self.assertEqual(
            self.host.allowed_ssh_ips, '198.51.100.9, 203.0.113.7, 77.42.42.201',
        )

    def test_a_refusal_changes_nothing_here(self):
        with self.assertRaises(UserError):
            self._change('remove', ['203.0.113.7'],
                         answer='ERROR: refusing to leave nobody allowed',
                         status=1)
        self.assertEqual(self.host.allowed_ssh_ips, '203.0.113.7')

    def test_an_answer_without_the_marker_is_a_failure(self):
        with self.assertRaises(UserError):
            self._change('add', ['77.42.42.201'], answer='')

    def test_a_host_never_hardened_has_nothing_to_change(self):
        self.host.hardened = False
        with patch.object(type(self.host), 'get_transport') as opened:
            self.assertIsNone(
                self.host._ssh_allowlist_change('add', ['77.42.42.201']),
            )
        opened.assert_not_called()

    def test_only_add_and_remove(self):
        with self.assertRaises(UserError):
            self.host._ssh_allowlist_change('set', ['77.42.42.201'])
        with self.assertRaises(UserError):
            self.host._ssh_allowlist_change('add', [])

    def test_the_address_reaches_the_script_quoted(self):
        _result, transport = self._change('add', ['77.42.42.201; reboot'])
        run = next(
            c.args[0] for c in transport.run.await_args_list
            if 'ssh_allowlist.sh' in c.args[0]
        )
        self.assertIn("'77.42.42.201; reboot'", run)
