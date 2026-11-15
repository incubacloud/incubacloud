"""An SFTP backup destination: its URL, its key pair and what guards them.

The destination owns who and where (user, server, port); a DST only says
which folder. The key pair is the panel's, written by the server only,
and the private half is readable by the platform alone. What reaches a
host is pinned by fingerprints in the configuration snapshot.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import asyncssh

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Request
from odoo.tests.common import tagged

from odoo.addons.incubacloud.controllers._data_load import _routes_backends
from odoo.addons.incubacloud.models import sftp_dst

from .test_cloud_security import CloudSecurityBase

_CASES = json.loads(
    (Path(__file__).parent / "data" / "sftp_dst_cases.json").read_text()
)["cases"]

#: A server key line, as ``known_hosts`` holds it.
_KNOWN_HOSTS = (
    "backup.example.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)


class _SftpBase(CloudSecurityBase):

    def setUp(self):
        super().setUp()
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-dest",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_user": "u123456",
            "sftp_path": "backups",
        })


@tagged("post_install", "-at_install")
class TestSftpDst(_SftpBase):

    def test_the_case_table(self):
        """Every DST of the shared table reads as duplicity reads it."""
        for dst, expected in _CASES:
            with self.subTest(dst=dst):
                if expected is None:
                    with self.assertRaises(sftp_dst.DstError):
                        sftp_dst.parse(dst)
                else:
                    self.assertEqual(list(sftp_dst.parse(dst)), expected)

    def test_build_keeps_a_relative_path_relative(self):
        """With no path the DST ends at the host: appending stays relative."""
        self.assertEqual(sftp_dst.build("u", "h", 22, ""), "sftp://u@h")
        self.assertEqual(
            sftp_dst.build("u", "h", 23, "/srv/b/"), "sftp://u@h:23//srv/b",
        )
        user, host, port, path = sftp_dst.parse(
            sftp_dst.build("u", "h", 22, "") + "/p/i",
        )
        self.assertEqual(path, "p/i")

    def test_check_dst_refuses_another_server_port_or_user(self):
        """Who and where are the destination's: a DST only says which folder."""
        good = "sftp://u1@h.example.com/backups/p/i"
        self.assertEqual(
            sftp_dst.check_dst("u1", "H.example.com", 22, good), "backups/p/i",
        )
        for dst in (
            "sftp://u2@h.example.com/backups/p/i",
            "sftp://u1@other.example.com/backups/p/i",
            "sftp://u1@h.example.com:2222/backups/p/i",
        ):
            with self.subTest(dst=dst), self.assertRaises(sftp_dst.DstMismatch):
                sftp_dst.check_dst("u1", "h.example.com", 22, dst)

    def test_instance_path_needs_project_and_instance(self):
        self.assertTrue(sftp_dst.instance_path("backups/p/i"))
        self.assertTrue(sftp_dst.instance_path("p/i"))
        self.assertFalse(sftp_dst.instance_path("backups"))
        self.assertFalse(sftp_dst.instance_path(""))


@tagged("post_install", "-at_install")
class TestSftpBackendModel(_SftpBase):

    def test_create_generates_the_key_pair(self):
        """An Ed25519 pair: the private half usable, the public one a line."""
        b = self.backend.sudo()
        key = asyncssh.import_private_key(b.sftp_private_key)
        self.assertEqual(key.algorithm, b"ssh-ed25519")
        kind, _data, comment = b.sftp_public_key.split()
        self.assertEqual(kind, "ssh-ed25519")
        self.assertEqual(comment, "incubacloud-backup")
        self.assertTrue(b.passphrase)

    def test_backup_dst(self):
        self.assertEqual(
            self.backend.backup_dst, "sftp://u123456@backup.example.com/backups",
        )
        self.backend.write({"sftp_port": 23, "sftp_path": "/srv/copies"})
        self.assertEqual(
            self.backend.backup_dst,
            "sftp://u123456@backup.example.com:23//srv/copies",
        )
        self.backend.write({"sftp_path": ""})
        self.assertEqual(
            self.backend.backup_dst, "sftp://u123456@backup.example.com:23",
        )

    def test_values_refused(self):
        for vals in (
            {"sftp_host": "not a host"},
            {"sftp_host": "2001:db8::1"},
            {"sftp_port": 0},
            {"sftp_port": 70000},
            {"sftp_user": "bad user"},
            {"sftp_user": "x" * 101},
            {"sftp_path": "a/../b"},
            {"sftp_path": "a//b"},
            {"sftp_path": "a b"},
            {"sftp_path": "/"},
            {"retention_owner": "lifecycle"},
        ):
            with self.subTest(vals=vals), self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.backend.write(vals)

    def test_the_server_is_kept_lower_case_without_spaces(self):
        """duplicity looks the key up under the URL's host, lowered.

        A server typed with capitals or spaces would give a URL with
        spaces and a trusted line no copy finds: both are written as the
        URL names the server.
        """
        backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-caps", "backend_type": "sftp",
            "sftp_host": "  Backup.Example.COM ", "sftp_user": "u1",
            "sftp_path": "backups",
        })
        self.assertEqual(backend.sftp_host, "backup.example.com")
        self.assertEqual(
            backend.backup_dst, "sftp://u1@backup.example.com/backups",
        )
        key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        self.assertTrue(
            backend._sftp_presented_key_line(key).startswith("backup.example.com "),
        )
        backend.sudo().write({"sftp_known_hosts": _KNOWN_HOSTS})
        backend.write({"sftp_host": "BACKUP.example.com "})
        self.assertEqual(backend.sftp_host, "backup.example.com")
        # The same server: its trusted key stays.
        self.assertTrue(backend.sftp_host_key_fingerprint)

    def test_a_port_sent_as_text_is_the_number(self):
        """The page sends the port as text once it is touched."""
        self.backend.sudo().write({"sftp_known_hosts": _KNOWN_HOSTS})
        self.backend.write({"sftp_port": "22"})
        self.assertTrue(self.backend.sftp_host_key_fingerprint)
        self.backend.write({"sftp_port": " 23 "})
        self.assertEqual(self.backend.sftp_port, 23)
        self.assertFalse(self.backend.sftp_host_key_fingerprint)
        with self.assertRaises(ValidationError):
            self.backend.write({"sftp_port": "2x"})

    def test_a_user_of_100_characters_is_accepted(self):
        self.backend.write({"sftp_user": "a" * 100})

    def test_the_type_does_not_change(self):
        with self.assertRaises(UserError):
            self.backend.write({"backend_type": "s3"})

    def test_moving_the_endpoint_drops_the_trusted_key(self):
        self.backend.sudo().write({"sftp_known_hosts": _KNOWN_HOSTS})
        self.assertTrue(self.backend.sftp_host_key_fingerprint)
        self.backend.write({"sftp_user": "u654321"})
        self.assertTrue(self.backend.sftp_host_key_fingerprint)
        self.backend.write({"sftp_port": 23})
        self.assertFalse(self.backend.sudo().sftp_known_hosts)
        self.assertFalse(self.backend.sftp_host_key_fingerprint)

    def test_identity_is_locked_by_a_pinned_copy(self):
        """An archived or imported copy keeps the server, port and user."""
        project = self.env["cloud.project"].create({"name": "sftp-proj"})
        self.env["cloud.instance"].create({
            "name": "pinned", "project_id": project.id,
            "environment": "production",
            "backup_backend_id": self.backend.id,
            "custom_backup_dst": self.backend.backup_dst + "/p/pinned",
            "active": False,
        })
        for vals in (
            {"sftp_host": "other.example.com"},
            {"sftp_port": 23},
            {"sftp_user": "u2"},
        ):
            with self.subTest(vals=vals), self.assertRaises(UserError):
                self.backend.write(vals)
        # The folder is not an identity: it is the DST's to say.
        self.backend.write({"sftp_path": "other"})

    def test_a_manager_who_is_not_an_odoo_admin_creates_one(self):
        """A panel's manager is no Odoo administrator: the key is the server's."""
        manager = self._create_user("sftp_mgr_create", "group_cloud_manager")
        self.assertFalse(manager.has_group("base.group_system"))
        backend = self.env["cloud.backup.backend"].with_user(manager).create({
            "name": "by-manager", "backend_type": "sftp",
            "sftp_host": "h.example.com", "sftp_user": "u1",
        })
        self.assertTrue(backend.sftp_public_key)
        key = asyncssh.import_private_key(backend.sudo().sftp_private_key)
        self.assertEqual(key.algorithm, b"ssh-ed25519")
        with self.assertRaises(AccessError):
            backend.read(["sftp_private_key"])

    def test_server_fields_refused_to_a_manager(self):
        manager = self._create_user("sftp_mgr", "group_cloud_manager")
        as_manager = self.backend.with_user(manager)
        for field, value in (
            ("sftp_known_hosts", _KNOWN_HOSTS),
            ("sftp_public_key", "ssh-ed25519 AAAA x"),
            ("sftp_private_key", "-----BEGIN-----"),
            ("sftp_host_key_fingerprint", "SHA256:forged"),
        ):
            with self.subTest(field=field), self.assertRaises(UserError):
                as_manager.write({field: value})
        with self.assertRaises(UserError):
            self.env["cloud.backup.backend"].with_user(manager).create({
                "name": "x", "backend_type": "sftp",
                "sftp_host": "h.example.com", "sftp_user": "u",
                "sftp_known_hosts": _KNOWN_HOSTS,
            })
        # The private half is the platform's alone.
        with self.assertRaises(AccessError):
            as_manager.read(["sftp_private_key"])

    def test_copy_takes_no_key(self):
        dup = self.backend.copy({"name": "dup"})
        self.assertFalse(dup.sudo().sftp_known_hosts)
        self.assertFalse(dup.sftp_host_key_trusted_at)

    def test_an_unhandled_type_raises_instead_of_reading_empty(self):
        """Not knowing is not an empty destination (archive_copy_lost)."""
        with self.assertRaises(UserError):
            self.backend._measure_prefix(self.backend.backup_dst + "/p/i")
        with self.assertRaises(UserError):
            self.backend._measure_usage()

    def test_regenerate_key(self):
        before = self.backend.sftp_public_key
        self.backend._sftp_regenerate_key()
        self.assertNotEqual(self.backend.sftp_public_key, before)
        audit = self.env["cloud.audit.log"].search(
            [("action", "=", "SFTP backup key regenerated")],
        )
        self.assertTrue(audit)


@tagged("post_install", "-at_install")
class TestSftpSnapshot(_SftpBase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "snap-proj"})
        self.instance = self.env["cloud.instance"].create({
            "name": "snap", "project_id": self.project.id,
            "environment": "production",
            "backup_backend_id": self.backend.id,
        })

    def test_snapshot_carries_fingerprints_never_the_key(self):
        self.backend.sudo().write({"sftp_known_hosts": _KNOWN_HOSTS})
        snap = self.instance._render_config_snapshot()
        self.assertEqual(
            snap["backup_sftp"]["host_key"],
            self.backend.sftp_host_key_fingerprint,
        )
        raw = json.dumps(snap, default=str)
        private = self.backend.sudo().sftp_private_key
        self.assertNotIn(private.splitlines()[1], raw)
        self.assertNotIn(self.backend.sftp_public_key.split()[1], raw)

    def test_a_new_key_or_trusted_key_changes_the_hash(self):
        first = self.instance._config_snapshot_hash()
        self.backend._sftp_regenerate_key()
        second = self.instance._config_snapshot_hash()
        self.assertNotEqual(first, second)
        self.backend.sudo().write({"sftp_known_hosts": _KNOWN_HOSTS})
        self.assertNotEqual(second, self.instance._config_snapshot_hash())

    def test_an_s3_instance_hashes_as_before(self):
        s3 = self.env["cloud.backup.backend"].create({
            "name": "s3", "s3_bucket": "b", "s3_access_key_id": "a",
            "s3_secret_access_key": "s",
        })
        self.instance.backup_backend_id = s3
        self.assertNotIn("backup_sftp", self.instance._render_config_snapshot())


@tagged("post_install", "-at_install")
class TestSftpRoutes(_SftpBase):

    def setUp(self):
        super().setUp()
        self.controller = _routes_backends.BackendsMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]

    def _call(self, method, *args, **kwargs):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_backends, "request", fake_req):
            return getattr(self.controller, method)(*args, **kwargs)

    def test_get_returns_the_public_key_never_the_private(self):
        data = self._call("cloud_get_backup_backend", self.backend.id)
        self.assertEqual(data["sftp_public_key"], self.backend.sftp_public_key)
        self.assertFalse(data["sftp_host_key_trusted"])
        private = self.backend.sudo().sftp_private_key
        self.assertNotIn(private.splitlines()[1], json.dumps(data))
        self.assertNotIn("sftp_private_key", data)

    def test_save_ignores_server_fields(self):
        self._call("cloud_save_backup_backend", self.backend.id, {
            "sftp_known_hosts": _KNOWN_HOSTS, "sftp_path": "other",
        })
        self.assertEqual(self.backend.sftp_path, "other")
        self.assertFalse(self.backend.sudo().sftp_known_hosts)

    def test_a_manager_who_is_not_an_odoo_admin_creates_one_by_the_route(self):
        manager = self._create_user("sftp_mgr_route", "group_cloud_manager")
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env(user=manager)
        with patch.object(_routes_backends, "request", fake_req):
            data = self.controller.cloud_create_backup_backend({
                "name": "by-route", "backend_type": "sftp",
                "sftp_host": "H.example.com", "sftp_port": "2222",
                "sftp_user": "u1", "sftp_path": "backups",
            })
        backend = self.env["cloud.backup.backend"].browse(data["id"])
        self.assertEqual(
            backend.backup_dst, "sftp://u1@h.example.com:2222/backups",
        )
        self.assertTrue(backend.sudo().sftp_private_key)

    def test_regenerate_route(self):
        before = self.backend.sftp_public_key
        data = self._call("cloud_sftp_backend_regenerate_key", self.backend.id)
        self.assertTrue(data["ok"])
        self.assertNotEqual(data["sftp_public_key"], before)
