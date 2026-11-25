"""What the panel does on an SFTP destination's server: test, measure, sizes.

Every session logs in with the destination's key and accepts the trusted
server key only; a measure tells a folder that is not there (missing)
from a server it cannot reach (unreachable); and a refused server key is
told by comparing what the server presents with what was trusted, which
raises the alert only when the instance runs with the trusted key.
"""
import asyncio
import contextlib
import os
import shlex
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh

from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.incubacloud.controllers._data_load import _routes_backends
from odoo.addons.incubacloud.models import ssh_host_keys
from odoo.addons.incubacloud.models.backup_list_executor import BackupListExecutor
from odoo.addons.incubacloud.models.cloud_backup_backend import SftpUnreachable
from odoo.addons.incubacloud.models.transport import SSHTransport

from .test_detached_step import _local_execute

_PRESENTED = "odoo.addons.incubacloud.models.ssh_host_keys.presented_host_key"


def _name(filename, kind, size=0):
    """Return a directory entry as ``readdir`` hands it back."""
    return asyncssh.SFTPName(filename, attrs=asyncssh.SFTPAttrs(type=kind, size=size))


_DIR = asyncssh.FILEXFER_TYPE_DIRECTORY
_FILE = asyncssh.FILEXFER_TYPE_REGULAR
_LINK = asyncssh.FILEXFER_TYPE_SYMLINK


class _SftpUseCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The core's address, whatever a layer installed says (its own
        # policy has tests of its own).
        cls.startClassPatcher(patch.object(
            type(cls.env["cloud.backup.backend"]), "_sftp_address",
            autospec=True, side_effect=lambda self: self.sftp_host,
        ))

    def setUp(self):
        super().setUp()
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-use",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_user": "u1",
            "sftp_path": "backups",
        })
        self.server_key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        self.backend.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, self.server_key,
            ),
        })
        self.sftp = MagicMock(spec=asyncssh.SFTPClient)
        self.Backend = type(self.env["cloud.backup.backend"])

    @contextlib.contextmanager
    def _server(self, tree=None, connect_error=None):
        """Stand an SFTP client in for the server; *tree* maps folder → entries."""
        tree = tree or {}

        async def readdir(path):
            if path not in tree:
                raise asyncssh.SFTPNoSuchFile("no such file")
            return tree[path]

        self.sftp.readdir = AsyncMock(side_effect=readdir)
        sftp = self.sftp

        @contextlib.asynccontextmanager
        async def client(_self, _kwargs):
            if connect_error:
                raise SftpUnreachable(connect_error) from connect_error
            yield sftp

        with patch.object(self.Backend, "_sftp_client", client):
            yield sftp


@tagged("post_install", "-at_install")
class TestSftpSession(_SftpUseCase):

    def test_login_with_the_destination_key_and_only_the_trusted_key(self):
        kwargs = self.backend._sftp_connect_kwargs()
        self.assertEqual(kwargs["host"], "backup.example.com")
        self.assertEqual(kwargs["username"], "u1")
        trusted, cas, revoked = kwargs["known_hosts"]
        self.assertEqual(
            trusted[0].export_public_key("openssh"),
            self.server_key.export_public_key("openssh"),
        )
        self.assertEqual((cas, revoked), ([], []))
        self.assertIsNone(kwargs["agent_path"])

    def test_no_session_without_a_trusted_key(self):
        self.backend.write({"sftp_port": 23})
        with self.assertRaises(UserError):
            self.backend._sftp_connect_kwargs()

    def test_walk_counts_files_skips_links_and_recurses(self):
        tree = {
            "backups/p/i": [
                _name(".", _DIR), _name("..", _DIR),
                _name("duplicity-full.20261101T020000Z.vol1.difftar.gpg", _FILE, 100),
                _name("escape", _LINK),
                _name("sub", _DIR),
            ],
            "backups/p/i/sub": [_name("a", _FILE, 5)],
        }
        with self._server(tree):
            files = self.backend._sftp_scan("backups/p/i")
        self.assertEqual(sorted(size for _p, size in files), [5, 100])

    def test_measure_prefix_missing_vs_unreachable(self):
        dst = "sftp://u1@backup.example.com/backups/p/i"
        with self._server({}):
            self.assertEqual(self.backend._measure_prefix(dst), (0.0, 0))
        with self._server({"backups/p/i": [_name("x", _FILE, 7)]}):
            self.assertEqual(self.backend._measure_prefix(dst), (7.0, 1))
        with self._server(connect_error=OSError("down")):
            with self.assertRaises(SftpUnreachable):
                self.backend._measure_prefix(dst)

    def test_measure_prefix_off_the_destination_raises(self):
        """Another server, or a shared folder: unreachable, never missing."""
        with self._server({}):
            for dst in (
                "sftp://u1@other.example.com/backups/p/i",
                "sftp://u1@backup.example.com/backups",
            ):
                with self.subTest(dst=dst), self.assertRaises(ValueError):
                    self.backend._measure_prefix(dst)

    def test_measure_usage(self):
        with self._server({"backups": [_name("f", _FILE, 1024**3)]}):
            self.backend._measure_usage()
        self.assertAlmostEqual(self.backend.last_measured_gb, 1.0)

    def test_the_cron_measures_sftp_and_alerts_on_another_key(self):
        other = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        with self._server(connect_error=asyncssh.HostKeyNotVerifiable("no")), \
                patch(_PRESENTED, return_value=other):
            result = self.env["cloud.backup.backend"]._cron_measure_backup_usage()
        self.assertGreaterEqual(result["failed"], 1)
        alert = self.env["cloud.alert"].search([
            ("code", "=", "backup_sftp_host_key_changed"),
            ("dedup_key", "=", self.backend._alert_key()),
            ("state", "=", "active"),
        ])
        self.assertEqual(alert.level, "critical")

    def test_no_alert_when_the_key_is_the_trusted_one(self):
        with patch(_PRESENTED, return_value=self.server_key):
            self.assertEqual(self.backend._sftp_check_presented_key(), ("", ""))
        self.assertFalse(self.env["cloud.alert"].search([
            ("code", "=", "backup_sftp_host_key_changed"),
        ]))

    def _stand_probe(self, sftp, vfs):
        """Give *sftp* a folder, a file to write and remove, and *vfs* as statvfs."""
        sftp.makedirs = AsyncMock()
        handle = MagicMock(spec=asyncssh.SFTPClientFile)
        handle.write = AsyncMock()

        @contextlib.asynccontextmanager
        async def _open(path, mode):
            self.assertIn("backups/.incubacloud-probe-", path)
            self.assertEqual(mode, "w")
            yield handle

        sftp.open = _open
        sftp.remove = AsyncMock()
        sftp.statvfs = AsyncMock(**vfs)

    def test_connection_test_writes_and_removes_a_probe(self):
        # The real record, as asyncssh 2.24.0 names its fields: a mock told
        # ``f_bavail`` would answer to a wrong name.
        with self._server() as sftp:
            self._stand_probe(sftp, {"return_value": asyncssh.SFTPVFSAttrs(
                bavail=10, frsize=4096,
            )})
            result = self.backend._sftp_test()
        sftp.makedirs.assert_awaited_once_with("backups", exist_ok=True)
        sftp.remove.assert_awaited_once()
        self.assertEqual(result, {"free_bytes": 40960})

    def test_connection_test_reads_no_space_when_the_server_does_not_say(self):
        with self._server() as sftp:
            self._stand_probe(sftp, {"side_effect": asyncssh.SFTPOpUnsupported("no")})
            result = self.backend._sftp_test()
        sftp.remove.assert_awaited_once()
        self.assertEqual(result, {"free_bytes": None})


@tagged("post_install", "-at_install")
class TestSftpTestRoute(_SftpUseCase):

    def _call(self, method):
        controller = _routes_backends.BackendsMixin()
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_backends, "request", fake_req):
            return getattr(controller, method)(self.backend.id)

    def test_a_refused_key_shows_the_public_key(self):
        with self._server(connect_error=asyncssh.PermissionDenied("denied")), \
                patch(_PRESENTED, return_value=self.server_key):
            result = self._call("cloud_test_backup_backend")
        self.assertFalse(result["ok"])
        self.assertIn(self.backend.sftp_public_key, result["error"])

    def test_another_server_key_is_named(self):
        other = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        with self._server(connect_error=asyncssh.HostKeyNotVerifiable("no")), \
                patch(_PRESENTED, return_value=other):
            result = self._call("cloud_test_backup_backend")
        self.assertFalse(result["ok"])
        self.assertIn("not the trusted", result["error"])

    def test_a_trusted_rsa_key_still_presented_tells_the_real_cause(self):
        """The panel logs in with the RSA key: its failure is not the key's."""
        rsa = asyncssh.generate_private_key("ssh-rsa").convert_to_public()
        self.backend.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, rsa,
            ),
        })

        def presented(address, port, server_host_key_algs=()):
            """Return the RSA key to RSA alone, the Ed25519 one otherwise."""
            if tuple(server_host_key_algs) == ssh_host_keys.RSA_HOST_KEY_ORDER:
                return rsa
            return self.server_key

        with self._server(connect_error=asyncssh.PermissionDenied("denied")), \
                patch.object(
                    ssh_host_keys, "presented_host_key", autospec=True,
                    side_effect=presented,
                ):
            result = self._call("cloud_test_backup_backend")
        self.assertFalse(result["ok"])
        self.assertIn(self.backend.sftp_public_key, result["error"])
        self.assertNotIn("not the trusted", result["error"])

    def test_measure_route(self):
        with self._server({"backups": [_name("f", _FILE, 2048)]}):
            result = self._call("cloud_measure_backup_usage")
        self.assertTrue(result["ok"])


@tagged("post_install", "-at_install")
class TestSftpBackupList(_SftpUseCase):

    def setUp(self):
        super().setUp()
        project = self.env["cloud.project"].create({"name": "bl", "remote_folder": "p"})
        self.host = self.env["cloud.host"].create({
            "name": "bl-host", "ip_address": "192.0.2.94", "user": "u",
            "wildcard_domain": "bl.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "i", "project_id": project.id, "environment": "production",
            "host_id": self.host.id, "backup_backend_id": self.backend.id,
        })
        job_type = self.env["cloud.job.type"].search([("code", "=", "backup_list")], limit=1)
        job = self.env["cloud.job"].create({
            "name": "bl", "host_id": self.host.id, "instance_id": self.instance.id,
            "job_type_id": job_type.id,
        })
        self.executor = BackupListExecutor(job, self.host)

    def test_sizes_from_the_instance_folder(self):
        row = self.env["cloud.instance.backup"].create({
            "instance_id": self.instance.id, "kind": "chain",
            "backup_type": "Full", "backup_time": "2026-11-01 02:00:00",
        })
        tree = {"backups/p/i": [
            _name("duplicity-full.20261101T020000Z.vol1.difftar.gpg", _FILE, 300),
            _name("duplicity-full.20261101T020000Z.manifest.gpg", _FILE, 12),
        ]}
        with self._server(tree):
            asyncio.run(self.executor._populate_sizes())
        self.assertEqual(row.size, 312)

    #: What duplicity prints, on its error output, when the server key is
    #: not the trusted one.
    _DOES_NOT_MATCH = (
        "ssh connection to u1@backup.example.com:22 failed: Host key for "
        "server 'backup.example.com' does not match"
    )
    #: What duplicity's paramiko asks, on the output, of an unknown server.
    _UNKNOWN = (
        "The authenticity of host 'backup.example.com' can't be established."
    )

    def _refused(self, applied_host_key, presented, text=None, stream="stderr",
                 rsa=None):
        """Run the listing's own command here, with ``docker`` printing *text*.

        The command goes through ``bash`` as on the host, so what reaches
        ``on_failure`` is what the step's redirections let through. The
        server key is read for real from the job's running loop, down to
        ``asyncssh.get_server_host_key``, the only thing answered here.

        :param str applied_host_key: the fingerprint the instance runs with
        :param presented: the key the server presents now
        :param str text: what duplicity prints (``_DOES_NOT_MATCH``)
        :param str stream: ``stderr`` or ``stdout``, where it prints it
        :param rsa: the key the server presents to RSA alone, when it is
            not *presented*
        :return: the active changed-key alerts
        """
        self.instance.sudo().write({
            "applied_config_snapshot": {"backup_sftp": {"host_key": applied_host_key}},
        })
        text = text or self._DOES_NOT_MATCH
        with tempfile.TemporaryDirectory() as tmp:
            docker = Path(tmp, "docker")
            redirect = " >&2" if stream == "stderr" else ""
            docker.write_text(
                f"#!/bin/sh\necho {shlex.quote(text)}{redirect}\nexit 23\n",
            )
            docker.chmod(0o755)
            transport = MagicMock(spec=SSHTransport)
            transport.execute.side_effect = _local_execute

            async def _presented(*_args, server_host_key_algs=(), **_kwargs):
                """Return *rsa* to RSA alone when given, *presented* otherwise."""
                if rsa is not None and (
                    tuple(server_host_key_algs) == ssh_host_keys.RSA_HOST_KEY_ORDER
                ):
                    return rsa
                return presented

            async def _run():
                results, _skipped = await self.executor._run_steps(
                    transport, self.executor.get_commands(),
                )
                errors = self.executor.parse_results(results)
                self.assertTrue(errors)
                await self.executor.on_failure(results, errors)

            with patch.object(BackupListExecutor, "_inst_dir", return_value=tmp), \
                    patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"}), \
                    patch.object(
                        ssh_host_keys.asyncssh, "get_server_host_key",
                        autospec=True, side_effect=_presented,
                    ):
                asyncio.run(_run())
        return self.env["cloud.alert"].search([
            ("code", "=", "backup_sftp_host_key_changed"), ("state", "=", "active"),
        ])

    def test_an_instance_on_an_older_key_needs_a_build_not_an_alert(self):
        other = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        self.assertFalse(self._refused("SHA256:older", other))

    def test_an_instance_on_the_trusted_key_raises_the_alert(self):
        """duplicity's «does not match» comes on its error output."""
        other = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        self.assertTrue(
            self._refused(self.backend.sftp_host_key_fingerprint, other),
        )

    def test_an_unknown_server_raises_the_alert_too(self):
        """The question about an unknown server comes on the output."""
        other = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        self.assertTrue(self._refused(
            self.backend.sftp_host_key_fingerprint, other,
            text=self._UNKNOWN, stream="stdout",
        ))

    def test_a_trusted_rsa_key_still_presented_asks_for_the_other_type(self):
        """OpenSSH 8.8 and later: paramiko gets Ed25519 for an RSA line."""
        rsa = asyncssh.generate_private_key("ssh-rsa").convert_to_public()
        self.backend.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, rsa,
            ),
        })
        with patch.object(BackupListExecutor, "_sys", autospec=True) as said:
            alert = self._refused(
                self.backend.sftp_host_key_fingerprint, self.server_key, rsa=rsa,
            )
        self.assertIn("check its Ed25519 key", alert.message)
        self.assertNotIn("not the trusted", alert.message)
        lines = [c.args[1] for c in said.call_args_list]
        self.assertTrue(any("still presents unchanged" in line for line in lines))
        self.assertFalse(any("not the trusted" in line for line in lines))

    def test_a_trusted_rsa_key_replaced_is_a_changed_key(self):
        """Another RSA key presented to RSA alone: the changed-key alert."""
        rsa = asyncssh.generate_private_key("ssh-rsa").convert_to_public()
        self.backend.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, rsa,
            ),
        })
        alert = self._refused(
            self.backend.sftp_host_key_fingerprint, self.server_key,
            rsa=asyncssh.generate_private_key("ssh-rsa").convert_to_public(),
        )
        self.assertIn("not the trusted", alert.message)

    def test_the_trusted_key_presented_raises_nothing(self):
        self.assertFalse(self._refused(
            self.backend.sftp_host_key_fingerprint, self.server_key,
        ))

    def test_sizes_never_fail_the_job(self):
        """An SFTP error halfway, or a server that stops answering."""
        self.env["cloud.instance.backup"].create({
            "instance_id": self.instance.id, "kind": "chain",
            "backup_type": "Full", "backup_time": "2026-11-01 02:00:00",
        })
        with self._server() as sftp:
            sftp.readdir = AsyncMock(side_effect=asyncssh.SFTPFailure("halfway"))
            asyncio.run(self.executor._populate_sizes())

        async def _never(*_args):
            await asyncio.sleep(5)
            raise AssertionError("the size pass has no time limit")

        with self._server() as sftp, patch(
            "odoo.addons.incubacloud.models.backup_list_executor."
            "_SFTP_OPERATION_TIMEOUT", 0.05,
        ):
            sftp.readdir = _never
            asyncio.run(self.executor._populate_sizes())
