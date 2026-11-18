"""An SFTP destination that logs in with a password instead of a key.

The key stays the default; a password is for a server that accepts no
other way in. It is a manager's secret, never returned to the page, and
reaches the hosts in ``backup.env`` as duplicity's ``BACKEND_PASSWORD``
(duplicity 3.0.5.1's ``Backend.get_password``, read by its paramiko
backend), and the purges as the same variable. The server key is
trusted as for a key: still required. Switching between the two drops
the other secret, and leaves the instances with changes to apply.
"""
import asyncio
import hashlib
import json
from unittest.mock import MagicMock, patch

import asyncssh
import yaml

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Request
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.controllers._data_load import _routes_backends
from odoo.addons.incubacloud.models import (
    cloud_backup_backend,
    sftp_dst,
    ssh_host_keys,
)
from odoo.addons.incubacloud.models.cloud_backup_backend import (
    CloudBackupBackend,
    SftpUnreachable,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.purge_archived_executor import (
    SFTP_KEY_NAME,
    SFTP_KNOWN_HOSTS_NAME,
)
from odoo.addons.incubacloud.net.outbound import OutboundError

from .test_purge_archived import _ArchivedPurgeBase

_PRESENTED = "odoo.addons.incubacloud.models.ssh_host_keys.presented_host_key"

#: A password the server accepts, of characters compose reads as written.
_PASSWORD = "Correct-horse 9 battery!"


def _known_hosts(port=22):
    """Return a trusted ``known_hosts`` line for backup.example.com."""
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    return ssh_host_keys.known_hosts_line("backup.example.com", port, key)


class _PasswordCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-pw",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_user": "u1",
            "sftp_path": "backups",
            "sftp_auth_method": "password",
            "sftp_password": _PASSWORD,
        })

    def _trust(self, backend=None, port=22):
        (backend or self.backend).sudo().write({"sftp_known_hosts": _known_hosts(port)})


@tagged("post_install", "-at_install")
class TestSftpPasswordModel(_PasswordCase):

    def test_the_key_stays_the_default(self):
        backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-key", "backend_type": "sftp",
            "sftp_host": "backup.example.com", "sftp_user": "u1",
        })
        self.assertEqual(backend.sftp_auth_method, "key")
        self.assertTrue(backend.sftp_public_key)
        self.assertFalse(backend.sudo().sftp_password)

    def test_a_password_destination_has_no_key_pair(self):
        b = self.backend.sudo()
        self.assertEqual(b.sftp_password, _PASSWORD)
        self.assertFalse(b.sftp_private_key)
        self.assertFalse(b.sftp_public_key)

    def test_a_password_is_required(self):
        with self.assertRaises(ValidationError):
            self.env["cloud.backup.backend"].create({
                "name": "no-pw", "backend_type": "sftp",
                "sftp_host": "backup.example.com", "sftp_user": "u1",
                "sftp_auth_method": "password",
            })

    def test_a_password_compose_would_read_as_more_is_refused(self):
        """It is a line of ``backup.env``, which compose reads."""
        for password in (
            "pa$sword", 'pa"sswd', "pa'sswd", "pa`sswd", "pa\\sswd", "pa#sswd",
            " leading", "trailing ", "two\nlines", "tab\there",
        ):
            with self.subTest(password=password), self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.backend.write({"sftp_password": password})

    def test_a_password_of_fewer_than_8_characters_is_refused(self):
        """The agents' scrubber masks secrets of 8 characters or more."""
        with self.assertRaisesRegex(ValidationError, "at least 8 characters"):
            with self.env.cr.savepoint():
                self.backend.write({"sftp_password": "Short-7"})
        self.backend.write({"sftp_password": "Eight-ch"})
        self.assertEqual(self.backend.sudo().sftp_password, "Eight-ch")

    def test_saving_without_retyping_keeps_the_password(self):
        self.backend.write({"sftp_password": "", "sftp_path": "other"})
        self.assertEqual(self.backend.sudo().sftp_password, _PASSWORD)

    def test_a_manager_sets_it_and_cannot_read_it_back_from_the_page(self):
        self.assertIn("sftp_password", CloudBackupBackend._PASSWORD_FIELDS)
        field = self.env["cloud.backup.backend"]._fields["sftp_password"]
        self.assertEqual(field.groups, "incubacloud.group_cloud_manager")
        self.assertFalse(field.copy)

    def test_a_project_member_cannot_read_it(self):
        """Who reads the destination does not read its password."""
        member = new_test_user(
            self.env, login="sftp_pw_member",
            groups="base.group_user,incubacloud.group_cloud_user",
        )
        backend = self.backend.with_user(member)
        self.assertEqual(backend.sftp_auth_method, "password")
        with self.assertRaises(AccessError):
            backend.sftp_password  # noqa: B018
        with self.assertRaises(AccessError):
            backend.read(["sftp_password"])

    def test_switching_to_a_password_drops_the_key_pair(self):
        backend = self.env["cloud.backup.backend"].create({
            "name": "to-pw", "backend_type": "sftp",
            "sftp_host": "backup.example.com", "sftp_user": "u1",
        })
        self._trust(backend)
        backend.write({"sftp_auth_method": "password", "sftp_password": _PASSWORD})
        self.assertFalse(backend.sudo().sftp_private_key)
        self.assertFalse(backend.sftp_public_key)
        # The server key is the server's: it stays trusted.
        self.assertTrue(backend.sftp_host_key_fingerprint)
        self.assertTrue(self.env["cloud.audit.log"].search([
            ("action", "=", "SFTP login changed"),
            ("details", "ilike", f"(id {backend.id})"),
        ]))

    def test_a_manager_who_is_not_an_odoo_admin_switches_both_ways(self):
        """The key pair is the platform's: the switch drops and makes it."""
        manager = new_test_user(
            self.env, login="sftp_pw_mgr",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        self.assertFalse(manager.has_group("base.group_system"))
        backend = self.env["cloud.backup.backend"].with_user(manager).create({
            "name": "by-manager", "backend_type": "sftp",
            "sftp_host": "backup.example.com", "sftp_user": "u1",
        })
        backend.write({"sftp_auth_method": "password", "sftp_password": _PASSWORD})
        self.assertFalse(backend.sudo().sftp_private_key)
        self.assertEqual(backend.sftp_password, _PASSWORD)
        backend.write({"sftp_auth_method": "key"})
        self.assertTrue(backend.sudo().sftp_private_key)
        self.assertFalse(backend.sudo().sftp_password)

    def test_switching_to_a_password_needs_one(self):
        backend = self.env["cloud.backup.backend"].create({
            "name": "to-pw-empty", "backend_type": "sftp",
            "sftp_host": "backup.example.com", "sftp_user": "u1",
        })
        with self.assertRaises(ValidationError):
            backend.write({"sftp_auth_method": "password"})

    def test_switching_to_a_key_drops_the_password_and_generates_a_pair(self):
        self._trust()
        self.backend.write({"sftp_auth_method": "key"})
        b = self.backend.sudo()
        self.assertFalse(b.sftp_password)
        key = asyncssh.import_private_key(b.sftp_private_key)
        self.assertEqual(key.algorithm, b"ssh-ed25519")
        self.assertTrue(b.sftp_public_key.endswith(" incubacloud-backup"))
        self.assertTrue(b.sftp_host_key_fingerprint)

    def test_a_key_destination_keeps_no_password(self):
        backend = self.env["cloud.backup.backend"].create({
            "name": "key-pw", "backend_type": "sftp",
            "sftp_host": "backup.example.com", "sftp_user": "u1",
            "sftp_password": _PASSWORD,
        })
        self.assertFalse(backend.sudo().sftp_password)
        backend.write({"sftp_password": _PASSWORD})
        self.assertFalse(backend.sudo().sftp_password)

    def test_no_key_to_regenerate(self):
        with self.assertRaises(UserError):
            self.backend._sftp_regenerate_key()


@tagged("post_install", "-at_install")
class TestSftpPasswordSnapshot(_PasswordCase):

    def setUp(self):
        super().setUp()
        self._trust()
        project = self.env["cloud.project"].create({"name": "pw-snap"})
        self.instance = self.env["cloud.instance"].create({
            "name": "snap", "project_id": project.id,
            "environment": "production",
            "backup_backend_id": self.backend.id,
        })

    def test_a_new_password_leaves_changes_to_apply_never_the_password(self):
        snap = self.instance._render_config_snapshot()
        self.assertEqual(snap["backup_sftp"]["auth"], "password")
        self.assertNotIn(_PASSWORD, json.dumps(snap, default=str))
        first = self.instance._config_snapshot_hash()
        self.backend.write({"sftp_password": "Another-one 2"})
        self.assertNotEqual(first, self.instance._config_snapshot_hash())

    def test_the_fingerprint_needs_the_database_secret(self):
        """Project members read the applied snapshot: nothing in it may be
        checked against a guessed password without the database's secret,
        which only the platform reads."""
        fingerprint = self.instance._render_config_snapshot()["backup_sftp"]["password"]
        unkeyed = [
            hashlib.new(name, raw.encode()).hexdigest()
            for raw in (
                _PASSWORD,
                json.dumps(_PASSWORD),
                json.dumps([self.backend.id, _PASSWORD]),
                f"{self.backend.id}:{_PASSWORD}",
            )
            for name in ("md5", "sha1", "sha256", "sha512", "blake2b")
        ]
        self.assertFalse([d for d in unkeyed if fingerprint in d])
        self.env["ir.config_parameter"].sudo().set_param(
            "database.secret", "another database's secret",
        )
        self.assertNotEqual(
            fingerprint,
            self.instance._render_config_snapshot()["backup_sftp"]["password"],
        )

    def test_the_same_password_saved_again_leaves_nothing_to_apply(self):
        """The hosts hold the password, not its encrypted form."""
        first = self.instance._config_snapshot_hash()
        self.backend.write({"sftp_password": _PASSWORD})
        self.assertEqual(first, self.instance._config_snapshot_hash())

    def test_switching_leaves_changes_to_apply(self):
        first = self.instance._config_snapshot_hash()
        self.backend.write({"sftp_auth_method": "key"})
        self.assertNotEqual(first, self.instance._config_snapshot_hash())

    def test_a_key_destination_hashes_as_before(self):
        """The fleet on keys must not all come out with changes to apply."""
        self.backend.write({"sftp_auth_method": "key"})
        self.assertEqual(
            set(self.instance._render_config_snapshot()["backup_sftp"]),
            {"public", "host_key"},
        )


@tagged("post_install", "-at_install")
class TestSftpPasswordSession(_PasswordCase):

    def setUp(self):
        super().setUp()
        self._trust()

    def test_login_with_the_password_and_only_the_trusted_key(self):
        with patch.object(
            type(self.env["cloud.backup.backend"]), "_sftp_address", autospec=True,
            side_effect=lambda self: self.sftp_host,
        ):
            kwargs = self.backend._sftp_connect_kwargs()
        self.assertEqual(kwargs["password"], _PASSWORD)
        # None, not []: asyncssh loads ~/.ssh's keys for an empty list.
        self.assertIsNone(kwargs["client_keys"])
        self.assertIsNone(kwargs["agent_path"])
        trusted, cas, revoked = kwargs["known_hosts"]
        self.assertEqual(len(trusted), 1)
        self.assertEqual((cas, revoked), ([], []))

    def test_public_servers_only_still_holds_with_a_password(self):
        """A layer's ``public`` policy refuses a password login to a server
        that resolves to a private address, as it refuses a key's."""
        Backend = type(self.env["cloud.backup.backend"])
        with patch.object(Backend, "_sftp_network_policy", autospec=True,
                          return_value="public"), \
                patch.object(
                    cloud_backup_backend, "_resolve_public", autospec=True,
                    side_effect=OutboundError("private"),
                ), self.assertRaisesRegex(UserError, "public addresses only"):
            self.backend._sftp_connect_kwargs()

    def test_still_no_session_without_a_trusted_key(self):
        self.backend.write({"sftp_port": 23})
        with self.assertRaises(UserError):
            self.backend._sftp_connect_kwargs()

    def test_a_refused_password_says_so_and_names_no_key(self):
        controller = _routes_backends.BackendsMixin()
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        server_key = asyncssh.import_public_key(
            " ".join(self.backend.sudo().sftp_known_hosts.split()[1:3]),
        )
        refused = SftpUnreachable("denied")
        refused.__cause__ = asyncssh.PermissionDenied("denied")
        with patch.object(_routes_backends, "request", fake_req), \
                patch.object(
                    type(self.env["cloud.backup.backend"]), "_sftp_run",
                    autospec=True, side_effect=refused,
                ), \
                patch(_PRESENTED, return_value=server_key):
            result = controller.cloud_test_backup_backend(self.backend.id)
        self.assertFalse(result["ok"])
        self.assertIn("password", result["error"])
        self.assertNotIn("authorized_keys", result["error"])

    def test_the_page_gets_the_method_and_whether_a_password_is_set(self):
        controller = _routes_backends.BackendsMixin()
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_backends, "request", fake_req):
            data = controller.cloud_get_backup_backend(self.backend.id)
            controller.cloud_save_backup_backend(self.backend.id, {
                "sftp_password": "From-the page 3",
            })
        self.assertEqual(data["sftp_auth_method"], "password")
        self.assertTrue(data["has_sftp_password"])
        self.assertNotIn(_PASSWORD, json.dumps(data))
        self.assertNotIn("sftp_password", data)
        self.assertEqual(self.backend.sudo().sftp_password, "From-the page 3")


class _PasswordDeployCase(_PasswordCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({
            "name": "PwDeploy", "remote_folder": "proj",
        })
        self.host = self.env["cloud.host"].create({
            "name": "sftp-pw-host",
            "ip_address": "192.0.2.95",
            "user": "ubuntu",
            "wildcard_domain": "pw.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "pwinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "backup_backend_id": self.backend.id,
        })

    def _executor(self):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "deploy_instance")], limit=1,
        )
        job = self.env["cloud.job"].create({
            "name": "pw deploy",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": job_type.id,
        })
        executor = DeployInstanceExecutor(job, self.host)
        executor._host_cpus = 0
        return executor


@tagged("post_install", "-at_install")
class TestSftpPasswordDeploy(_PasswordDeployCase):

    def test_backup_env_carries_the_password_for_duplicity(self):
        self._trust()
        content = self._executor()._backup_env_content()
        self.assertIn(f"BACKEND_PASSWORD={_PASSWORD}\n", content)
        self.assertNotIn("FTP_PASSWORD", content)

    def test_a_key_destination_s_backup_env_has_none(self):
        self._trust()
        self.backend.write({"sftp_auth_method": "key"})
        content = self._executor()._backup_env_content()
        self.assertNotIn("PASSWORD", content)

    def test_the_answers_never_carry_it(self):
        answers = self.instance._render_copier_answers()
        self.assertEqual(answers["backup_backend_password"], "")
        self.assertNotIn(_PASSWORD, json.dumps(answers, default=str))

    def test_no_build_without_a_confirmed_server_key(self):
        with self.assertRaisesRegex(UserError, "Confirm the SFTP server's key"):
            self.instance._extra_env_files()

    def test_the_trusted_key_and_the_config_without_an_identity(self):
        self._trust()
        files = self.instance._extra_env_files()
        self.assertEqual(
            set(files), {sftp_dst.SSH_KNOWN_HOSTS_FILE, sftp_dst.SSH_CONFIG_FILE},
        )
        config = files[sftp_dst.SSH_CONFIG_FILE]
        self.assertNotIn("IdentityFile", config)
        self.assertIn("StrictHostKeyChecking yes", config)
        self.assertNotIn(_PASSWORD, json.dumps(files))

    def test_the_directory_is_mounted_and_an_old_key_removed(self):
        """A destination switched from a key leaves no key on the host."""
        self._trust()
        executor = self._executor()
        d = "~/projects/x"
        steps = executor._write_extra_env_step(d)
        self.assertEqual(steps[0][0], "Ready the backup SSH directory")
        self.assertEqual(
            steps[0][1],
            f"mkdir -p {d}/.docker/backup-ssh && chmod 700 {d}/.docker/backup-ssh"
            f" && rm -f {d}/.docker/backup-ssh/id_ed25519",
        )
        self.assertNotIn("id_ed25519", steps[1][1])
        data = yaml.safe_load(executor._resource_override_content())
        self.assertIn(sftp_dst.SSH_MOUNT, data["services"]["backup"]["volumes"])

    def test_a_key_destination_s_directory_step_is_unchanged(self):
        self._trust()
        self.backend.write({"sftp_auth_method": "key"})
        d = "~/projects/x"
        steps = self._executor()._write_extra_env_step(d)
        self.assertEqual(
            steps[0][1],
            f"mkdir -p {d}/.docker/backup-ssh && chmod 700 {d}/.docker/backup-ssh",
        )


@tagged("post_install", "-at_install")
class TestSftpPasswordArchivedPurge(_ArchivedPurgeBase):

    def setUp(self):
        super().setUp()
        self.sftp = self.env["cloud.backup.backend"].create({
            "name": "pa-sftp-pw",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_user": "u1",
            "sftp_path": "backups",
            "sftp_auth_method": "password",
            "sftp_password": _PASSWORD,
        })
        self.sftp.sudo().write({"sftp_known_hosts": _known_hosts()})
        self.instance.backup_backend_id = self.sftp
        self.instance.project_id.remote_folder = "proj"

    def _job_executor(self):
        inst = self._archive()
        job_id = inst.delete_archived()[0]
        return self._executor(self.env["cloud.job"].browse(job_id))

    def test_the_environment_carries_the_password(self):
        executor = self._job_executor()
        (content,) = executor._env_contents().values()
        lines = content.splitlines()
        self.assertEqual(lines[0], "DST=sftp://u1@backup.example.com/backups/proj/pa-inst")
        self.assertIn(f"BACKEND_PASSWORD={_PASSWORD}", lines)
        self.assertNotIn("AWS_", content)

    def test_only_the_trusted_key_goes_beside_it(self):
        executor = self._job_executor()
        uploaded = {}

        async def upload(_executor, _transport, files):
            uploaded.update(files)

        with patch.object(type(executor), "_upload_private_files",
                          autospec=True, side_effect=upload):
            asyncio.run(executor.before_execute(None))
        names = {path.rsplit("/", 1)[-1] for path in uploaded}
        self.assertEqual(names, {"purge.env", SFTP_KNOWN_HOSTS_NAME})
        self.assertNotIn(SFTP_KEY_NAME, names)
