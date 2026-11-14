"""An SFTP destination's server key: read without credentials, trusted on sight.

The panel reads what the server presents with the key exchange alone,
shows its fingerprint, and stores it only when the person confirms the
one they saw and the server still presents it. The line is filed under
the server as the URL names it, which is what duplicity's paramiko
looks up on the host.
"""
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


def _server_key():
    """Return a fresh server key, as asyncssh hands one back."""
    return asyncssh.generate_private_key("ssh-ed25519").convert_to_public()


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
        presented.assert_called_once_with("backup.example.com", 23)
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
        env = self.env(context=dict(self.env.context, force_rate_limit=True))
        for _i in range(20):
            self.assertTrue(self._call(
                env, "cloud_sftp_backend_read_host_key", self.backend.id,
            )["ok"])
        denied = self._call(env, "cloud_sftp_backend_read_host_key", self.backend.id)
        self.assertFalse(denied["ok"])
        self.assertIn("Too many", denied["error"])
