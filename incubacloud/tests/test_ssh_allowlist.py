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

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport


class TestTheSshAllowlistChangesLive(TransactionCase):

    def setUp(self):
        super().setUp()
        # The list is kept on a cursor of its own: test mode makes it ride
        # on this transaction, so the write is seen and rolled back.
        self.registry_enter_test_mode()
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

    def test_the_list_is_kept_on_a_cursor_of_its_own(self):
        """Not on the caller's transaction, which may be long and whose
        snapshot made the write a serialization conflict (QA, 20-oct-2026)."""
        from odoo.addons.incubacloud.models import cloud_host
        with patch.object(
            cloud_host, 'read_committed_cursor', wraps=cloud_host.read_committed_cursor,
        ) as durable:
            self._change('add', ['77.42.42.201'])
        durable.assert_called_once_with(self.env.registry)
        self.assertEqual(self.host.allowed_ssh_ips, '203.0.113.7, 77.42.42.201')

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


class TestTheAllowlistHoldsAddresses(TransactionCase):
    """``allowed_ssh_ips`` holds addresses and networks, in one form.

    The playbook and ``ssh_allowlist.sh`` put each entry in an nft set
    and compare entries as text. An entry that is not an address stopped
    a hardening at ``nft -c``, and the same address written two ways
    (``2001:0db8::7``, ``2001:db8::7``) was two entries to the script: a
    removal named one and left the other.
    """

    def setUp(self):
        """A host with no list yet; the list is kept on a cursor of its
        own, which test mode makes ride on this transaction."""
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env['cloud.host'].create({
            'name': 'al-norm-host', 'ip_address': '192.0.2.61', 'user': 'ubuntu',
            'wildcard_domain': 'al-norm.example.com',
        })

    def test_an_entry_that_is_not_an_address_is_refused(self):
        """Named in the message, so it can be fixed where it was typed."""
        for value in (
            '203.0.113.7, unknown', '203.0.113.300', '2001:db8::7;reboot',
            # A zone names an interface of the machine it was written on;
            # nft takes no such address.
            'fe80::1%eth0',
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError) as caught:
                self.host.write({'allowed_ssh_ips': value})
            self.assertIn(value.split(', ')[-1], str(caught.exception))

    def test_entries_are_kept_in_one_form(self):
        """An address compressed, a network with its prefix, one comma
        between entries, each once."""
        self.host.write({
            'allowed_ssh_ips': '2001:0DB8::7, 10.0.0.1/8\n198.51.100.4 2001:db8::7',
        })
        self.assertEqual(
            self.host.allowed_ssh_ips, '2001:db8::7, 10.0.0.0/8, 198.51.100.4',
        )
        host = self.env['cloud.host'].create({
            'name': 'al-norm-host-2', 'ip_address': '192.0.2.62', 'user': 'ubuntu',
            'wildcard_domain': 'al-norm2.example.com',
            'allowed_ssh_ips': '2001:0db8:0000::0007',
        })
        self.assertEqual(host.allowed_ssh_ips, '2001:db8::7')

    def test_a_network_of_one_address_is_that_address(self):
        """``/32`` and ``/128`` name one address: kept without a prefix,
        or ``remove 203.0.113.7`` left ``203.0.113.7/32`` admitting it."""
        for value, kept in (
            ('203.0.113.7/32, 203.0.113.7', '203.0.113.7'),
            ('2001:db8::7/128', '2001:db8::7'),
            ('0.0.0.0/0, ::/0', '0.0.0.0/0, ::/0'),
        ):
            with self.subTest(value=value):
                self.host.write({'allowed_ssh_ips': value})
                self.assertEqual(self.host.allowed_ssh_ips, kept)

    def test_an_ipv4_mapped_address_is_kept_as_ipv4(self):
        """In the IPv6 set it would never match: the packets it stands
        for arrive over IPv4."""
        self.host.write({'allowed_ssh_ips': '::ffff:203.0.113.7, ::ffff:198.51.100.0/120'})
        self.assertEqual(self.host.allowed_ssh_ips, '203.0.113.7, 198.51.100.0/24')

    def test_an_empty_list_and_the_catch_alls_are_accepted(self):
        """Empty means open, and the host's script may hand back either
        catch-all."""
        for value in (False, '', '0.0.0.0/0, ::/0'):
            with self.subTest(value=value):
                self.host.write({'allowed_ssh_ips': value})
        self.assertEqual(self.host.allowed_ssh_ips, '0.0.0.0/0, ::/0')

    def test_a_list_stored_before_the_check_does_not_block_the_host(self):
        """Checked when it is written, not when the record is read or
        another field is saved."""
        self.env.cr.execute(
            "UPDATE cloud_host SET allowed_ssh_ips = 'unknown' WHERE id = %s",
            [self.host.id],
        )
        self.host.invalidate_recordset(['allowed_ssh_ips'])
        self.assertEqual(self.host.allowed_ssh_ips, 'unknown')
        self.host.write({'name': 'al-norm-host-renamed'})

    def _script_command(self, op, ips, answer):
        """Run ``_ssh_allowlist_change`` and return the script's command.

        :return: the command line, and what the host now allows
        :rtype: tuple
        """
        self.host.hardened = True
        transport = MagicMock(spec=SSHTransport)
        transport.run = AsyncMock(side_effect=lambda cmd, **kw: (
            CommandResult(f'IC_SSH_ALLOW {answer}', 0)
            if 'ssh_allowlist.sh' in cmd else CommandResult('', 0)
        ))
        transport.upload_text_files = AsyncMock()

        @asynccontextmanager
        async def _open(host):
            yield transport

        with patch.object(type(self.host), 'get_transport', _open):
            self.host._ssh_allowlist_change(op, ips)
        return next(
            c.args[0] for c in transport.run.await_args_list
            if 'ssh_allowlist.sh' in c.args[0]
        )

    def test_the_script_is_given_the_form_the_host_holds(self):
        """``remove 2001:0db8::7`` takes out ``2001:db8::7``."""
        command = self._script_command(
            'remove', ['2001:0DB8::0007'], '203.0.113.7',
        )
        self.assertIn(' remove 2001:db8::7 ', command)
        command = self._script_command('remove', ['198.51.100.4/32'], '203.0.113.7')
        self.assertIn(' remove 198.51.100.4 ', command)

    def test_what_the_host_answers_is_kept_in_one_form(self):
        """The script prints the list as the host's file holds it; the
        field keeps it in the panel's form."""
        self._script_command(
            'add', ['77.42.42.201'], '2001:0db8::7, 77.42.42.201',
        )
        self.assertEqual(self.host.allowed_ssh_ips, '2001:db8::7, 77.42.42.201')
