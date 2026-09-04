"""The three ways a backup archive reaches a host.

Chunked upload exists because a proxy in front of the panel caps a
single request long before Odoo sees it — Cloudflare at 100 MB, nginx at
whatever ``client_max_body_size`` says — so the one-shot upload answered
413 to anything larger and the size the interface promised was fiction.

The temporary key exists because the panel holds credentials to hosts
the person restoring has none for, and printing an rsync command that
assumes otherwise is not a flow, it is a dead end.

Fetching from a link exists because neither of those helps with an
archive that already lives somewhere else.
"""
import json

from odoo.addons.incubacloud.net.outbound import OutboundError
from odoo.addons.incubacloud.net.restore_source import (
    curl_resolve_argument,
    masked,
    split_credentials,
    validate,
)
from odoo.addons.incubacloud.restore_staging import (
    is_staged_upload,
    new_upload_path,
    path_for_upload,
    upload_id_of,
)
from odoo.tests.common import BaseCase, TransactionCase


class TestStagedUploadTokens(BaseCase):

    def test_a_token_round_trips_to_the_same_path(self):
        path = new_upload_path(42)
        token = upload_id_of(path)
        self.assertTrue(token)
        self.assertEqual(path_for_upload(42, token), path)

    def test_a_token_cannot_name_another_instance(self):
        """The path is rebuilt from the instance, not from the caller."""
        path = new_upload_path(42)
        token = upload_id_of(path)
        self.assertNotEqual(path_for_upload(43, token), path)
        self.assertFalse(is_staged_upload(str(path), 43))

    def test_a_token_cannot_walk_out_of_the_staging_area(self):
        for bad in ("../../etc/passwd", "a/b", "", "zz", None,
                    "0123456789abcdef0"):
            with self.subTest(token=bad):
                self.assertIsNone(path_for_upload(42, bad))

    def test_a_foreign_name_yields_no_token(self):
        self.assertEqual(upload_id_of("/tmp/something-else.tar"), "")


class TestRestoreSourceUrls(BaseCase):

    def test_credentials_are_split_off_the_url(self):
        clean, user, password = split_credentials(
            "sftp://bob:hunter2@files.example.com/backup.zip",
        )
        self.assertEqual(clean, "sftp://files.example.com/backup.zip")
        self.assertEqual(user, "bob")
        self.assertEqual(password, "hunter2")

    def test_a_url_without_credentials_is_left_alone(self):
        clean, user, password = split_credentials("https://example.com/b.zip")
        self.assertEqual(clean, "https://example.com/b.zip")
        self.assertEqual((user, password), ("", ""))

    def test_the_displayed_form_never_carries_the_password(self):
        shown = masked("ftp://bob:hunter2@files.example.com/backup.zip")
        self.assertNotIn("hunter2", shown)
        self.assertIn("bob:***@", shown)

    def test_only_three_schemes_are_accepted(self):
        for url in ("file:///etc/passwd", "gopher://x/1",
                    "http://example.com/b.zip"):
            with self.subTest(url=url):
                with self.assertRaises(OutboundError):
                    validate(url)

    def test_credentials_left_in_the_url_are_refused(self):
        """They must be split off first, so they cannot be logged."""
        with self.assertRaises(OutboundError):
            validate("sftp://bob:hunter2@files.example.com/b.zip")

    def test_a_private_address_is_refused(self):
        for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "[::1]"):
            with self.subTest(host=host):
                with self.assertRaises(OutboundError):
                    validate(f"https://{host}/backup.zip")

    def test_the_resolved_address_is_pinned_for_curl(self):
        parts, address, port = validate("https://93.184.216.34/backup.zip")
        pin = curl_resolve_argument(parts, address, port)
        self.assertEqual(pin, "93.184.216.34:443:93.184.216.34")

    def test_an_ipv6_pin_is_bracketed(self):
        parts, _address, port = validate("https://[2606:2800:220:1::]/b.zip")
        pin = curl_resolve_argument(parts, "2606:2800:220:1::", port)
        self.assertIn("[2606:2800:220:1::]", pin)


class TestRestoreModeGate(TransactionCase):
    """``restore_db`` decides what a payload is allowed to ask for."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "restore-host",
            "ip_address": "198.51.100.20",
            "user": "root",
            "wildcard_domain": "test.example",
        })
        project = self.env["cloud.project"].create({"name": "restore-proj"})
        self.instance = self.env["cloud.instance"].create({
            "name": "restore-inst",
            "project_id": project.id,
            "host_id": self.host.id,
            "odoo_version": "19.0",
        })

    def test_an_unknown_mode_is_refused(self):
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.instance.restore_db({"mode": "whatever"})

    def test_ssh_upload_needs_a_grant_that_belongs_here(self):
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.instance.restore_db({"mode": "ssh_upload", "grant_id": 0})

    def test_ssh_upload_needs_the_upload_to_have_been_checked(self):
        """Restoring what nobody looked at defeats the confirmation.

        The digest computed on the host is the only thing that
        distinguishes the archive the operator sent from anything else
        that could have been written into the directory.
        """
        from odoo.exceptions import UserError
        grant, _key = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        with self.assertRaises(UserError):
            self.instance.restore_db({
                "mode": "ssh_upload", "grant_id": grant.id,
            })

    def test_a_from_url_restore_stores_the_credential_apart(self):
        """Never in the payload: that is read back into the interface."""
        job_id = self.instance.restore_from_url(
            "https://93.184.216.34/backup.zip",
        )
        job = self.env["cloud.job"].browse(job_id)
        self.assertEqual(job.payload["mode"], "from_url")
        self.assertFalse(job.secret_payload)

    def test_a_link_to_a_private_address_is_refused(self):
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.instance.restore_from_url("https://127.0.0.1/backup.zip")


class TestUploadGrant(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "grant-host",
            "ip_address": "198.51.100.21",
            "user": "root",
            "wildcard_domain": "test.example",
        })
        project = self.env["cloud.project"].create({"name": "grant-proj"})
        self.instance = self.env["cloud.instance"].create({
            "name": "grant-inst",
            "project_id": project.id,
            "host_id": self.host.id,
            "odoo_version": "19.0",
        })

    def test_the_private_key_is_returned_and_not_stored(self):
        grant, private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        self.assertIn("PRIVATE KEY", private)
        self.env.cr.execute(
            "SELECT * FROM cloud_restore_upload_grant WHERE id = %s",
            (grant.id,),
        )
        row = self.env.cr.dictfetchone()
        for value in row.values():
            self.assertNotIn("PRIVATE KEY", str(value))

    def test_the_key_line_carries_every_restriction(self):
        grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        line = grant._authorized_keys_line()
        self.assertIn('command="/usr/bin/rrsync -wo ', line)
        self.assertIn("restrict", line)
        self.assertIn("expiry-time=", line)
        self.assertIn(grant._key_comment(), line)
        self.assertIn(grant.token, line)

    def test_the_directory_is_the_grant_s_own(self):
        grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        self.assertTrue(grant._directory().endswith(grant.token))
        self.assertIn(grant.token, grant._remote_path())

    def test_revoking_is_idempotent(self):
        grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        self.assertEqual(len(grant._revoke()), 1)
        self.assertEqual(grant.state, "revoked")
        self.assertEqual(grant._revoke(), [])

    def test_the_sweep_revokes_what_has_expired(self):
        from odoo import fields
        grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        grant.expires_at = fields.Datetime.now().replace(year=2020)
        self.env["cloud.restore.upload.grant"]._gc_grants()
        self.assertEqual(grant.state, "revoked")

    def test_a_fingerprint_is_recorded(self):
        grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        self.assertTrue(grant.fingerprint.startswith("SHA256:"))


class TestJobSecretPayload(TransactionCase):

    def test_a_credential_is_encrypted_at_rest(self):
        host = self.env["cloud.host"].create({
            "name": "secret-host", "ip_address": "198.51.100.22",
            "user": "root",
            "wildcard_domain": "test.example",
        })
        job = self.env["cloud.job"].create({
            "name": "x",
            "host_id": host.id,
            "job_type_id": self.env.ref("incubacloud.host_probe").id,
        })
        job.secret_payload = json.dumps({
            "machine": "files.example.com",
            "login": "bob",
            "password": "hunter2",
        })
        self.env.cr.execute(
            "SELECT secret_payload FROM cloud_job WHERE id = %s", (job.id,),
        )
        self.assertNotIn("hunter2", self.env.cr.fetchone()[0] or "")
        self.assertEqual(
            job._restore_url_credential(),
            ("files.example.com", "bob", "hunter2"),
        )

    def test_no_credential_reads_as_none(self):
        host = self.env["cloud.host"].create({
            "name": "secret-host-2", "ip_address": "198.51.100.23",
            "user": "root",
            "wildcard_domain": "test.example",
        })
        job = self.env["cloud.job"].create({
            "name": "x",
            "host_id": host.id,
            "job_type_id": self.env.ref("incubacloud.host_probe").id,
        })
        self.assertIsNone(job._restore_url_credential())
