"""The SSH key the panel generates for a host, and the public half it shows.

A new host can be given a key the panel makes instead of one the person
uploads: the private half is sealed until the host is created, then kept
in ``key_file`` exactly as an uploaded one. The host page shows the public
half of whatever key is stored, and never the private one.
"""

import base64
from unittest.mock import MagicMock, patch

import asyncssh
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user

from .test_cloud_host import TestSaveHostCertificateRoute


def _uploaded_rsa_key():
    """Return an RSA key as a person would upload it, and its public half.

    RSA in the classic PEM envelope, so the derivation is shown to work for
    a key the panel did not make.

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
    return base64.b64encode(pem).decode(), public


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
