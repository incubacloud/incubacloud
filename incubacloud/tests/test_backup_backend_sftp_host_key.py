"""An SFTP destination's server key: read without credentials, trusted on sight.

The panel reads what the server presents with the key exchange alone,
shows its fingerprint, and stores it only when the person confirms the
one they saw and the server still presents it. The line is filed under
the server as the URL names it, which is what duplicity's paramiko
looks up on the host.
"""
import asyncio
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import asyncssh

from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.incubacloud.controllers._data_load import _routes_backends
from odoo.addons.incubacloud.models import cloud_backup_backend, ssh_host_keys
from odoo.addons.incubacloud.net.outbound import OutboundError

_PRESENTED = "odoo.addons.incubacloud.models.ssh_host_keys.presented_host_key"


def _core_address(cls):
    """Pin the address the panel dials to the core's answer, the name itself.

    A layer's CI may run these tests with a policy of its own (public
    addresses only), which has tests of its own.
    """
    cls.startClassPatcher(patch.object(
        type(cls.env["cloud.backup.backend"]), "_sftp_address",
        autospec=True, side_effect=lambda self: self.sftp_host,
    ))


def _server_key(kind="ssh-ed25519"):
    """Return a fresh server key of *kind*, as asyncssh hands one back."""
    return asyncssh.generate_private_key(kind).convert_to_public()


@contextmanager
def _loopback_server(host_keys):
    """Run a real asyncssh server on 127.0.0.1, on a free port.

    It runs on an event loop of its own in a thread, so the synchronous
    :func:`ssh_host_keys.presented_host_key` can dial it with its own loop.
    It only exchanges keys: no session is opened.

    :param list host_keys: the server's host key pairs
        (``asyncssh.load_keypairs``), each offered with its
        ``host_key_algorithms``
    :return: the port it listens on
    :rtype: int
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def _await(awaitable):
        """Await *awaitable*: ``asyncssh.listen`` returns no coroutine."""
        return await awaitable

    async def _close(server):
        """Close *server* on its own loop: asyncio is not thread-safe."""
        server.close()
        await server.wait_closed()

    def _on_loop(awaitable):
        """Await *awaitable* on the server's loop and return its result."""
        return asyncio.run_coroutine_threadsafe(_await(awaitable), loop).result(
            ssh_host_keys.TOTAL_TIMEOUT,
        )

    try:
        server = _on_loop(asyncssh.listen(
            "127.0.0.1", 0, server_host_keys=host_keys,
        ))
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            _on_loop(_close(server))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(ssh_host_keys.TOTAL_TIMEOUT)
        loop.close()


def _empty_client_key_home(home):
    """Give *home* the empty ``~/.ssh/id_rsa`` a Doodba project ships.

    :param str home: a temporary folder to stand in for ``$HOME``
    """
    ssh = Path(home, ".ssh")
    ssh.mkdir()
    for name in ("id_rsa", "id_rsa.pub"):
        Path(ssh, name).touch()


@tagged("post_install", "-at_install")
class TestSshHostKeys(TransactionCase):

    def test_prefix_and_fingerprint(self):
        self.assertEqual(ssh_host_keys.known_hosts_prefix("h", 22), "h")
        self.assertEqual(ssh_host_keys.known_hosts_prefix("h", 2222), "[h]:2222")
        key = _server_key()
        line = ssh_host_keys.known_hosts_line("h", 23, key)
        self.assertTrue(line.startswith("[h]:23 ssh-ed25519 "))
        self.assertEqual(ssh_host_keys.key_type(line), "ssh-ed25519")
        self.assertEqual(
            ssh_host_keys.fingerprint(line),
            self.env["cloud.host"]._known_hosts_fingerprint(line),
        )
        self.assertEqual(ssh_host_keys.fingerprint("# nothing"), "")

    def test_the_key_types_reach_asyncssh_as_its_own_keyword(self):
        """Given in ``options`` they would be overridden: the keyword is used."""
        key = _server_key()
        with patch.object(
            ssh_host_keys.asyncssh, "get_server_host_key",
            autospec=True, return_value=key,
        ) as scan:
            found = ssh_host_keys.presented_host_key(
                "h", 23, server_host_key_algs=ssh_host_keys.PARAMIKO_HOST_KEY_ORDER,
            )
        self.assertIs(found, key)
        scan.assert_called_once()
        self.assertEqual(scan.call_args.args, ("h", 23))
        self.assertEqual(
            scan.call_args.kwargs["server_host_key_algs"],
            ssh_host_keys.PARAMIKO_HOST_KEY_ORDER,
        )
        self.assertEqual(ssh_host_keys.PARAMIKO_HOST_KEY_ORDER[0], "ssh-ed25519")
        self.assertNotIn(
            "server_host_key_algs", scan.call_args.kwargs["options"].kwargs,
        )

    def test_paramiko_puts_the_trusted_type_first(self):
        """An ``ssh-rsa`` or ECDSA line moves its type to the front; Ed25519 is there."""
        order = ssh_host_keys.PARAMIKO_HOST_KEY_ORDER
        self.assertEqual(ssh_host_keys.paramiko_order(""), order)
        self.assertEqual(ssh_host_keys.paramiko_order("ssh-ed25519"), order)
        rsa_first = ssh_host_keys.paramiko_order("ssh-rsa")
        self.assertEqual(rsa_first[0], "ssh-rsa")
        self.assertEqual(rsa_first[1:], order[:-1])
        self.assertEqual(
            ssh_host_keys.paramiko_order("ecdsa-sha2-nistp256")[:2],
            ("ecdsa-sha2-nistp256", "ssh-ed25519"),
        )

    def test_a_server_without_ssh_rsa_answers_paramiko_order_with_ed25519(self):
        """A real exchange with a server that offers RSA as OpenSSH 8.8 does.

        The server has an RSA and an Ed25519 key and offers ``rsa-sha2-*``
        but not ``ssh-rsa``: asked in paramiko's order for an RSA line, it
        answers with its Ed25519 key, the one the backups compare; asked
        for RSA alone, with its RSA key. An asyncssh server offers what its
        key pairs list, so the RSA pair drops ``ssh-rsa`` from its list.
        """
        rsa, ed = asyncssh.load_keypairs([
            asyncssh.generate_private_key("ssh-rsa"),
            asyncssh.generate_private_key("ssh-ed25519"),
        ])
        rsa.host_key_algorithms = tuple(
            alg for alg in rsa.host_key_algorithms if alg != b"ssh-rsa"
        )
        self.assertIn(b"rsa-sha2-512", rsa.host_key_algorithms)
        with _loopback_server([rsa, ed]) as port:
            paramiko = ssh_host_keys.presented_host_key(
                "127.0.0.1", port,
                server_host_key_algs=ssh_host_keys.paramiko_order("ssh-rsa"),
            )
            rsa_alone = ssh_host_keys.presented_host_key(
                "127.0.0.1", port,
                server_host_key_algs=ssh_host_keys.RSA_HOST_KEY_ORDER,
            )
        self.assertEqual(paramiko.public_data, ed.key_public_data)
        self.assertEqual(rsa_alone.public_data, rsa.key_public_data)

    def test_no_key_types_keeps_asyncssh_order(self):
        """An empty tuple is asyncssh's default; a list would select nothing."""
        with patch.object(
            ssh_host_keys.asyncssh, "get_server_host_key",
            autospec=True, return_value=_server_key(),
        ) as scan:
            ssh_host_keys.presented_host_key("h", 22)
        self.assertEqual(scan.call_args.kwargs["server_host_key_algs"], ())

    def test_no_client_key_is_loaded(self):
        """An empty ``~/.ssh/id_rsa`` stops asyncssh's own keys, not the reading."""
        key = _server_key()
        with tempfile.TemporaryDirectory() as home:
            _empty_client_key_home(home)
            with patch.dict(os.environ, {"HOME": home}):
                with self.assertRaises(asyncssh.KeyImportError):
                    asyncssh.SSHClientConnectionOptions()
                with patch.object(
                    ssh_host_keys.asyncssh, "get_server_host_key",
                    autospec=True, return_value=key,
                ) as scan:
                    self.assertIs(ssh_host_keys.presented_host_key("h", 22), key)
        options = scan.call_args.kwargs["options"]
        self.assertIsNone(options.kwargs["client_keys"])
        self.assertIsNone(options.kwargs["agent_path"])


@tagged("post_install", "-at_install")
class TestSftpHostKeyTrust(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _core_address(cls)

    def setUp(self):
        super().setUp()
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-hk",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_port": 23,
            "sftp_user": "u1",
        })
        self.key = _server_key()

    def _line(self, key=None):
        return ssh_host_keys.known_hosts_line(
            "backup.example.com", 23, key or self.key,
        )

    def test_read_dials_the_destination_and_labels_with_its_name(self):
        with patch(_PRESENTED, return_value=self.key) as presented:
            data = self.backend._sftp_read_host_key()
        presented.assert_called_once_with(
            "backup.example.com", 23,
            server_host_key_algs=ssh_host_keys.PARAMIKO_HOST_KEY_ORDER,
        )
        self.assertEqual(data["fingerprint"], ssh_host_keys.fingerprint(self._line()))
        self.assertEqual(data["key_type"], "ssh-ed25519")
        self.assertEqual(data["trusted_fingerprint"], "")
        self.assertFalse(self.backend.sudo().sftp_known_hosts)

    def test_trust_the_key_that_was_seen(self):
        seen = ssh_host_keys.fingerprint(self._line())
        self.env["cloud.alert"].raise_alert(
            "backup_sftp_host_key_changed", "changed",
            key=self.backend._alert_key(),
        )
        with patch(_PRESENTED, return_value=self.key):
            self.assertEqual(self.backend._sftp_trust_host_key(seen), seen)
        self.assertEqual(self.backend.sudo().sftp_known_hosts, self._line())
        self.assertEqual(self.backend.sftp_host_key_fingerprint, seen)
        self.assertTrue(self.backend.sftp_host_key_trusted_at)
        self.assertEqual(self.backend.sftp_host_key_trusted_by, self.env.user)
        self.assertFalse(self.env["cloud.alert"].search([
            ("code", "=", "backup_sftp_host_key_changed"),
            ("dedup_key", "=", self.backend._alert_key()),
            ("state", "=", "active"),
        ]))

    def test_another_key_in_between_is_refused(self):
        seen = ssh_host_keys.fingerprint(self._line())
        # Not assertRaises, whose savepoint would roll the audit back: the
        # route answers the refusal and its transaction keeps the entry.
        with patch(_PRESENTED, return_value=_server_key()):
            try:
                self.backend._sftp_trust_host_key(seen)
            except UserError:
                pass
            else:
                self.fail("another key was trusted")
        self.assertFalse(self.backend.sudo().sftp_known_hosts)
        self.assertTrue(self.env["cloud.audit.log"].search(
            [("action", "=", "SFTP server key refused")],
        ))

    def test_an_empty_approval_trusts_nothing(self):
        with patch(_PRESENTED, return_value=self.key):
            with self.assertRaises(UserError):
                self.backend._sftp_trust_host_key("")

    def test_a_key_type_paramiko_does_not_read_is_refused(self):
        odd = MagicMock(spec=asyncssh.SSHKey)
        odd.export_public_key.return_value = b"ssh-dss AAAAB3NzaC1kc3M=\n"
        with patch(_PRESENTED, return_value=odd):
            with self.assertRaises(UserError):
                self.backend._sftp_read_host_key()

    def test_both_readings_ask_in_paramiko_order_down_to_asyncssh(self):
        """The destination's key is the one duplicity's paramiko gets."""
        with patch.object(
            ssh_host_keys.asyncssh, "get_server_host_key",
            autospec=True, return_value=self.key,
        ) as scan:
            self.backend._sftp_read_host_key()
            asyncio.run(self.backend._sftp_presented_line_async())
        self.assertEqual(scan.call_count, 2)
        for call in scan.call_args_list:
            self.assertEqual(call.args, ("backup.example.com", 23))
            self.assertEqual(
                call.kwargs["server_host_key_algs"],
                ssh_host_keys.PARAMIKO_HOST_KEY_ORDER,
            )
            self.assertIsNone(call.kwargs["options"].kwargs["client_keys"])
            self.assertIsNone(call.kwargs["options"].kwargs["agent_path"])

    def test_a_client_key_asyncssh_cannot_import_is_told(self):
        """A ``KeyImportError`` (a ``ValueError``) comes back as a UserError."""
        error = asyncssh.KeyImportError("Invalid private key")
        with patch.object(
            ssh_host_keys, "presented_host_key", autospec=True,
            side_effect=error,
        ), self.assertRaisesRegex(UserError, "Invalid private key"):
            self.backend._sftp_read_host_key()
        with patch.object(
            ssh_host_keys, "presented_host_key_async", autospec=True,
            side_effect=error,
        ), self.assertRaises(UserError):
            asyncio.run(self.backend._sftp_presented_line_async())

    def test_unreachable_server(self):
        with patch(_PRESENTED, side_effect=OSError("refused")):
            with self.assertRaises(UserError):
                self.backend._sftp_read_host_key()

    def test_public_policy_dials_the_checked_address(self):
        """The core's own ``_sftp_address`` under a ``public`` policy."""
        Backend = type(self.env["cloud.backup.backend"])
        core = cloud_backup_backend.CloudBackupBackend._sftp_address
        with patch.object(Backend, "_sftp_network_policy", return_value="public"), \
                patch.object(
                    cloud_backup_backend, "_resolve_public",
                    return_value="203.0.113.9",
                ):
            self.assertEqual(core(self.backend), "203.0.113.9")
        with patch.object(Backend, "_sftp_network_policy", return_value="public"), \
                patch.object(
                    cloud_backup_backend, "_resolve_public",
                    side_effect=OutboundError("private"),
                ), self.assertRaises(UserError):
            core(self.backend)
        with patch.object(Backend, "_sftp_network_policy", return_value="any"):
            self.assertEqual(core(self.backend), "backup.example.com")


@tagged("post_install", "-at_install")
class TestSftpHostKeyRoutes(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _core_address(cls)

    def setUp(self):
        super().setUp()
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-hk-route",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_user": "u1",
        })
        self.controller = _routes_backends.BackendsMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.key = _server_key()

    def _call(self, env, method, *args):
        fake_req = MagicMock(spec=Request)
        fake_req.env = env
        with patch.object(_routes_backends, "request", fake_req), \
                patch(_PRESENTED, return_value=self.key):
            return getattr(self.controller, method)(*args)

    def test_read_then_trust(self):
        data = self._call(
            self.env, "cloud_sftp_backend_read_host_key", self.backend.id,
        )
        self.assertTrue(data["ok"])
        trusted = self._call(
            self.env, "cloud_sftp_backend_trust_host_key", self.backend.id,
            data["fingerprint"],
        )
        self.assertTrue(trusted["ok"])
        self.assertEqual(trusted["sftp_host_key_fingerprint"], data["fingerprint"])

    def test_an_s3_destination_has_no_server_key(self):
        s3 = self.env["cloud.backup.backend"].create({"name": "s3", "s3_bucket": "b"})
        data = self._call(self.env, "cloud_sftp_backend_read_host_key", s3.id)
        self.assertFalse(data["ok"])

    def test_twenty_connections_an_hour(self):
        env = self.env(context=self.env.context | {"force_rate_limit": True})
        for _i in range(20):
            self.assertTrue(self._call(
                env, "cloud_sftp_backend_read_host_key", self.backend.id,
            )["ok"])
        denied = self._call(env, "cloud_sftp_backend_read_host_key", self.backend.id)
        self.assertFalse(denied["ok"])
        self.assertIn("Too many", denied["error"])


@tagged("post_install", "-at_install")
class TestSftpTrustedRsaKey(TransactionCase):
    """A destination that trusts the server's RSA key.

    duplicity's paramiko asks first for the trusted line's type, ``ssh-rsa``,
    which OpenSSH 8.8 and later no longer offer, and gets the server's
    Ed25519 key, so the backups fail with "does not match". The panel reads
    in paramiko's order too, and then with RSA alone: the same RSA key says
    the key did not change, only its type must; another one is a change.
    """

    @classmethod
    def setUpClass(cls):
        """Generate the server's RSA and Ed25519 keys; dial the name itself."""
        super().setUpClass()
        _core_address(cls)
        cls.rsa = _server_key("ssh-rsa")
        cls.ed = _server_key()

    def setUp(self):
        """Create a destination that trusts the server's RSA key."""
        super().setUp()
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-rsa",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_user": "u1",
        })
        self.backend.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, self.rsa,
            ),
        })

    def _server(self, rsa):
        """Answer as a server with :attr:`ed` and *rsa*, without ``ssh-rsa`` first.

        :param rsa: the RSA key it presents to RSA alone, or an exception
        :return: the patcher of ``presented_host_key``
        """
        ed = self.ed

        def presented(address, port, server_host_key_algs=()):
            """Return *rsa* to RSA alone (or raise it), :attr:`ed` otherwise."""
            if tuple(server_host_key_algs) == ssh_host_keys.RSA_HOST_KEY_ORDER:
                if isinstance(rsa, Exception):
                    raise rsa
                return rsa
            return ed

        return patch.object(
            ssh_host_keys, "presented_host_key", autospec=True,
            side_effect=presented,
        )

    def _alert(self):
        """Return the destination's active server key alert."""
        return self.env["cloud.alert"].search([
            ("code", "=", "backup_sftp_host_key_changed"),
            ("dedup_key", "=", self.backend._alert_key()),
            ("state", "=", "active"),
        ])

    def _ed_fingerprint(self):
        """Return the fingerprint of the server's Ed25519 key."""
        return ssh_host_keys.fingerprint(
            ssh_host_keys.known_hosts_line("backup.example.com", 22, self.ed),
        )

    def test_the_same_rsa_key_asks_for_the_other_type(self):
        """The trusted RSA key still presented: the alert asks for Ed25519."""
        with self._server(self.rsa) as presented:
            outcome = self.backend._sftp_check_presented_key()
        self.assertEqual(outcome, ("key_type", self._ed_fingerprint()))
        self.assertEqual(
            [c.kwargs["server_host_key_algs"] for c in presented.call_args_list],
            [ssh_host_keys.paramiko_order("ssh-rsa"), ssh_host_keys.RSA_HOST_KEY_ORDER],
        )
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("check its Ed25519 key", alert.message)
        self.assertIn("still presents", alert.message)
        self.assertNotIn("not the trusted", alert.message)

    def test_another_rsa_key_is_a_changed_key(self):
        """Another RSA key than the trusted one is the changed-key alert."""
        with self._server(_server_key("ssh-rsa")):
            outcome = self.backend._sftp_check_presented_key()
        self.assertEqual(outcome, ("changed", self._ed_fingerprint()))
        self.assertIn("not the trusted", self._alert().message)

    def test_no_rsa_key_left_is_a_changed_key(self):
        """A server that no longer presents an RSA key: a changed key."""
        with self._server(OSError("no matching host key type")):
            outcome = self.backend._sftp_check_presented_key()
        self.assertEqual(outcome[0], "changed")
        self.assertIn("not the trusted", self._alert().message)

    def test_the_async_check_tells_the_same(self):
        """The check inside an event loop gives the same outcome and alert."""
        rsa = self.rsa

        async def presented(address, port, server_host_key_algs=()):
            """Return the RSA key to RSA alone, :attr:`ed` otherwise."""
            if tuple(server_host_key_algs) == ssh_host_keys.RSA_HOST_KEY_ORDER:
                return rsa
            return self.ed

        with patch.object(
            ssh_host_keys, "presented_host_key_async", autospec=True,
            side_effect=presented,
        ):
            outcome = asyncio.run(self.backend._sftp_check_presented_key_async())
        self.assertEqual(outcome, ("key_type", self._ed_fingerprint()))
        self.assertTrue(self._alert())

    def test_an_ed25519_key_trusted_is_never_read_again(self):
        """Only an RSA line is read again with RSA alone."""
        self.backend.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, _server_key(),
            ),
        })
        with self._server(self.rsa) as presented:
            outcome = self.backend._sftp_check_presented_key()
        self.assertEqual(outcome[0], "changed")
        presented.assert_called_once()

    def test_a_server_that_still_offers_ssh_rsa_raises_nothing(self):
        """Before OpenSSH 8.8 paramiko gets the RSA key: the backups work."""
        rsa, ed = self.rsa, self.ed

        def presented(address, port, server_host_key_algs=()):
            """Return the RSA key when ``ssh-rsa`` is asked first."""
            return rsa if server_host_key_algs[0] == "ssh-rsa" else ed

        with patch.object(
            ssh_host_keys, "presented_host_key", autospec=True,
            side_effect=presented,
        ) as scan:
            self.assertEqual(self.backend._sftp_check_presented_key(), ("", ""))
        scan.assert_called_once()
        self.assertFalse(self._alert())

    def test_a_trusted_ecdsa_key_still_offered_raises_nothing(self):
        """paramiko asks for ECDSA first, so the server's Ed25519 is no change."""
        ecdsa = _server_key("ecdsa-sha2-nistp256")
        ed = self.ed
        self.backend.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, ecdsa,
            ),
        })

        def presented(address, port, server_host_key_algs=()):
            """Return the ECDSA key when ECDSA is asked first."""
            return ecdsa if server_host_key_algs[0].startswith("ecdsa") else ed

        with patch.object(
            ssh_host_keys, "presented_host_key", autospec=True,
            side_effect=presented,
        ):
            self.assertEqual(self.backend._sftp_check_presented_key(), ("", ""))
        self.assertFalse(self._alert())

    def test_the_page_reads_and_trusts_the_other_type(self):
        """The destination's page reads the Ed25519 key and trusts it."""
        with self._server(self.rsa):
            self.backend._sftp_check_presented_key()
            data = self.backend._sftp_read_host_key()
            self.assertEqual(data["key_type"], "ssh-ed25519")
            self.assertEqual(data["fingerprint"], self._ed_fingerprint())
            self.backend._sftp_trust_host_key(data["fingerprint"])
            self.assertEqual(
                self.backend._sftp_check_presented_key(), ("", ""),
            )
        self.assertEqual(
            ssh_host_keys.key_type(self.backend.sudo().sftp_known_hosts),
            "ssh-ed25519",
        )
        self.assertFalse(self._alert())
