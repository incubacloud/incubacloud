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
import asyncio
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from odoo.addons.incubacloud.models import restore_instance_executor
from odoo.addons.incubacloud.models.restore_instance_executor import (
    RestoreInstanceExecutor,
)
from odoo.addons.incubacloud.models.restore_upload_key_executors import (
    GrantRestoreUploadKeyExecutor,
    VerifyRestoreUploadExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.net import restore_source
from odoo.addons.incubacloud.net.outbound import OutboundError
from odoo.addons.incubacloud.net.restore_source import (
    curl_resolve_argument,
    curl_url,
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
from odoo.addons.incubacloud.tests.test_executor_durable_writes import (
    JobEndAssertions,
)
from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase, TransactionCase, new_test_user, tagged

#: Links a host must never be told to fetch: private, loopback, the cloud
#: metadata service, a local file.
FORBIDDEN_LINKS = (
    "https://10.0.0.1/backup.zip",
    "https://127.0.0.1/backup.zip",
    "https://[::1]/backup.zip",
    "https://169.254.169.254/latest/meta-data/",
    "file:///etc/passwd",
)

#: A public address, and the pin ``restore_from_url`` computes for it.
PUBLIC_LINK = "https://93.184.216.34/backup.zip"
PUBLIC_PIN = "93.184.216.34:443:93.184.216.34"

#: Internationalised names: Python (IDNA2003) and the hosts' curl
#: (IDNA2008) would turn each into a different ASCII name.
NON_ASCII_LINKS = (
    "https://straße.de/backup.zip",
    "https://bücher.example/backup.zip",
    "sftp://ﬁles.example.com/backup.zip",
)


def _public_resolution():
    """Have every name resolve to one public address, without DNS.

    :return: the patcher, to be used as a context manager
    """
    return patch.object(
        restore_source, "_resolve_public", autospec=True,
        return_value="93.184.216.34",
    )


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

    def test_escaped_credentials_are_sent_as_they_are_meant(self):
        """A user or password with an ``@``, a ``:`` or a ``/`` can only be
        written into a link escaped; curl must receive it unescaped."""
        clean, user, password = split_credentials(
            "sftp://bob%40corp:p%40ss%3Aw%2Fx%25@files.example.com/b.zip",
        )
        self.assertEqual(clean, "sftp://files.example.com/b.zip")
        self.assertEqual(user, "bob@corp")
        self.assertEqual(password, "p@ss:w/x%")

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

    def test_a_name_that_is_not_plain_ascii_is_refused(self):
        """Python and the host's curl would each turn it into an ASCII
        name of their own: the one checked would not be the one fetched.
        Refused before anything resolves it, saying what to write."""
        with _public_resolution() as resolve:
            for url in NON_ASCII_LINKS:
                with self.subTest(url=url):
                    with self.assertRaisesRegex(OutboundError, "xn--"):
                        validate(url)
            resolve.assert_not_called()

    def test_a_name_with_what_no_host_name_holds_is_refused(self):
        """A percent sign, a space: curl and Python need not read it alike."""
        with _public_resolution() as resolve:
            for url in ("https://exa%41mple.com/b.zip",
                        "https://exa%2emple.com/b.zip"):
                with self.subTest(url=url):
                    with self.assertRaises(OutboundError):
                        validate(url)
            resolve.assert_not_called()

    def test_a_punycode_name_passes(self):
        with _public_resolution():
            parts, address, port = validate("https://xn--strae-oqa.de/b.zip")
        self.assertEqual(
            curl_resolve_argument(parts, address, port),
            "xn--strae-oqa.de:443:93.184.216.34",
        )
        self.assertEqual(curl_url(parts), "https://xn--strae-oqa.de/b.zip")

    def test_curl_fetches_the_very_name_it_pins(self):
        """The link curl is given names, byte for byte, the host its
        ``--resolve`` pins: otherwise curl would look the name up itself.
        A fragment is never sent."""
        for url, expected in (
            ("https://Files.Example.com/a/b.zip?x=1#part",
             "https://files.example.com/a/b.zip?x=1"),
            ("sftp://files.example.com:2222/b.zip",
             "sftp://files.example.com:2222/b.zip"),
            ("ftp://files.example.com./b.zip",
             "ftp://files.example.com./b.zip"),
        ):
            with self.subTest(url=url), _public_resolution():
                parts, address, port = validate(url)
                fetched = curl_url(parts)
                self.assertEqual(fetched, expected)
                pinned = curl_resolve_argument(parts, address, port)
                self.assertEqual(
                    urlsplit(fetched).netloc.rsplit(":", 1)[0]
                    if urlsplit(fetched).port else urlsplit(fetched).netloc,
                    pinned.split(":")[0],
                )
                self.assertEqual(pinned.split(":")[1], str(port))


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

    def test_restore_db_takes_no_link(self):
        """A link is checked by ``restore_from_url`` alone: ``restore_db``
        refuses one, whatever it names and whatever its context says, and
        queues nothing."""
        for url in (*FORBIDDEN_LINKS, PUBLIC_LINK):
            for instance in (
                self.instance, self.instance.with_context(restore_from_url=True),
            ):
                with self.subTest(url=url, context=dict(instance.env.context)):
                    before = self.env["cloud.job"].search_count([])
                    with self.assertRaisesRegex(UserError, "link"):
                        instance.restore_db({
                            "mode": "from_url", "url": url, "resolve": PUBLIC_PIN,
                        })
                    self.assertEqual(
                        self.env["cloud.job"].search_count([]), before,
                    )

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

    def test_a_link_with_a_non_ascii_name_is_refused(self):
        """The user is told to write the name's ``xn--`` form, and nothing
        is queued."""
        before = self.env["cloud.job"].search_count([])
        for url in NON_ASCII_LINKS:
            with self.subTest(url=url):
                with self.assertRaisesRegex(UserError, "xn--"):
                    self.instance.restore_from_url(url)
        self.assertEqual(self.env["cloud.job"].search_count([]), before)

    def _checked_grant(self):
        """Return a grant whose upload was checked."""
        grant, _key = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        grant.write({
            "received_filename": "restore.zip",
            "received_bytes": 5,
            "received_sha256": "ab" * 32,
        })
        return grant

    def test_ssh_upload_restores_only_the_digest_the_operator_saw(self):
        """The restore names the digest its dialog showed: none, or one
        another check has replaced since, queues nothing."""
        grant = self._checked_grant()
        before = self.env["cloud.job"].search_count([])
        for expected in (None, "", "cd" * 32):
            with self.subTest(expected=expected):
                payload = {"mode": "ssh_upload", "grant_id": grant.id}
                if expected is not None:
                    payload["expected_sha256"] = expected
                with self.assertRaisesRegex(UserError, "Check what arrived"):
                    self.instance.restore_db(payload)
        self.assertEqual(self.env["cloud.job"].search_count([]), before)
        job_id = self.instance.restore_db({
            "mode": "ssh_upload", "grant_id": grant.id,
            "expected_sha256": "ab" * 32,
        })
        self.assertTrue(job_id)


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
        self.assertIn('command="/usr/bin/rrsync -wo -munge -no-del ', line)
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

    def _used(self, days_ago, created_days_ago=None):
        """Return a grant a restore consumed *days_ago* days ago.

        :param int days_ago: age of its last change (``write_date``)
        :param int created_days_ago: age of the grant itself, the same
            by default
        """
        grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        grant._mark_used()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_restore_upload_grant"
            " SET write_date = now() at time zone 'UTC' - %s * interval '1 day',"
            " create_date = now() at time zone 'UTC' - %s * interval '1 day'"
            " WHERE id = %s",
            (days_ago, created_days_ago or days_ago, grant.id),
        )
        grant.invalidate_recordset()
        return grant

    def _revocations(self, grant):
        """Return the revocation jobs queued for *grant*."""
        return self.env["cloud.job"].search([
            ("job_type_id.code", "=", "revoke_restore_upload_key"),
            ("instance_id", "=", self.instance.id),
        ]).filtered(lambda job: job.payload.get("grant_id") == grant.id)

    def test_the_sweep_removes_what_a_consumed_grant_left_on_the_host(self):
        """A restore revokes the key and keeps the directory: a failed one
        gives the archive back there for its retry, a successful one
        leaves whatever else was sent. Three days on, the sweep revokes
        it again the usual way, directory and all; a newer one stays."""
        old = self._used(days_ago=4)
        recent = self._used(days_ago=1)
        self.env["cloud.restore.upload.grant"]._gc_grants()
        self.assertEqual(old.state, "revoked")
        self.assertEqual(recent.state, "used")
        (revocation,) = self._revocations(old)
        self.assertNotIn("keep_directory", revocation.payload)
        self.assertFalse(self._revocations(recent))
        self.assertEqual(old._revoke(), [])

    def test_a_grant_the_sweep_revokes_outlives_its_revocation(self):
        """The rows are purged a month after their last change, not their
        creation: the job that removes the directory must still find its
        grant."""
        old = self._used(days_ago=4, created_days_ago=40)
        self.env["cloud.restore.upload.grant"]._gc_grants()
        self.assertTrue(old.exists())
        self.assertTrue(self._revocations(old))

    def _migration(self):
        """Return the 1.0.200 post-migrate, loaded as Odoo loads it."""
        path = os.path.join(
            get_module_path("incubacloud"), "migrations", "1.0.200",
            "post-migrate.py",
        )
        spec = importlib.util.spec_from_file_location(
            "ic_post_migrate_1_0_200_grants", path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_an_upgrade_closes_the_grants_opened_before_it(self):
        """Their keys were installed by an older release: the upgrade hands
        them to the sweep, which runs soon after it and revokes them the
        usual way. Nothing reaches a host during the upgrade itself."""
        grants = self.env["cloud.restore.upload.grant"]
        opened, _private = grants._open(self.instance)
        closed, _private = grants._open(self.instance)
        closed._revoke()
        closed_deadline = closed.expires_at
        cron = self.env.ref("incubacloud.cron_cloud_restore_upload_grant_gc")
        # A copy of a database may hold its crons inactive; Odoo drops an
        # immediate trigger of an inactive one.
        cron.active = True
        triggers = self.env["ir.cron.trigger"].search_count(
            [("cron_id", "=", cron.id)],
        )
        jobs = self.env["cloud.job"].search_count([])
        self._migration().migrate(self.env.cr, "1.0.199")
        (opened | closed).invalidate_recordset()
        self.assertEqual(opened.state, "granted")
        self.assertLess(opened.expires_at, fields.Datetime.now())
        self.assertEqual(closed.expires_at, closed_deadline)
        self.assertEqual(self.env["cloud.job"].search_count([]), jobs)
        self.assertGreater(
            self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)]),
            triggers,
        )
        grants._gc_grants()
        self.assertEqual(opened.state, "revoked")

    def test_a_fresh_install_has_no_grant_to_close(self):
        opened, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        deadline = opened.expires_at
        self._migration().migrate(self.env.cr, None)
        opened.invalidate_recordset()
        self.assertEqual(opened.expires_at, deadline)

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


class TestTheConsumedGrantOutlivesTheJob(JobEndAssertions, TransactionCase):
    """Closing the key is the one thing a failed restore must not undo.

    It used to happen in the job's own transaction: a restore failing
    afterwards took the closure back, and even a successful one lost
    whatever the runner's final ``clear()`` found unflushed.
    """

    def setUp(self):
        """A restore job that consumes a checked upload grant."""
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "consumed-host",
            "ip_address": "198.51.100.22",
            "user": "root",
            "wildcard_domain": "consumed.example",
        })
        project = self.env["cloud.project"].create({"name": "consumed-proj"})
        self.instance = self.env["cloud.instance"].create({
            "name": "consumed-inst",
            "project_id": project.id,
            "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        self.grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.instance,
        )
        self.grant.write({
            "received_filename": "restore.zip",
            "received_sha256": "ab" * 32,
        })
        self.job = self.env["cloud.job"].create({
            "name": "restore",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env.ref("incubacloud.restore_instance").id,
            "payload": {
                "mode": "ssh_upload", "grant_id": self.grant.id,
                "expected_sha256": "ab" * 32,
            },
        })
        # The durable cursor reads the database, not this env's cache.
        self.env.flush_all()

    def test_the_grant_is_closed_and_its_revocation_queued_for_good(self):
        """Both the closure and the queued revocation survive the job's end."""
        executor = RestoreInstanceExecutor(self.job, self.host)
        transport = MagicMock(spec=SSHTransport)
        # The archive taken is the one that was checked.
        transport.run.side_effect = lambda command, *_a, **_kw: CommandResult(
            f"{'ab' * 32}  -\n" if command.startswith("sha256sum ") else "", 0,
        )
        asyncio.run(executor.before_execute(transport))
        self.end_job()
        self.assertEqual(self.stored(self.grant, "state"), "used")
        self.env.cr.execute(
            """
            SELECT j.queue_job_uuid IS NOT NULL
              FROM cloud_job j
              JOIN cloud_job_type t ON t.id = j.job_type_id
             WHERE t.code = 'revoke_restore_upload_key'
               AND j.instance_id = %s
               AND j.payload::text LIKE %s
            """,
            (self.instance.id, '%keep_directory%'),
        )
        rows = self.env.cr.fetchall()
        self.assertEqual(len(rows), 1, "exactly one revocation is queued")
        self.assertTrue(rows[0][0], "the revocation lost its queue job")


@tagged("-at_install", "post_install")
class TestTheJobSecretIsTheServers(TransactionCase):
    """A job's ``secret_payload`` is read by the server alone.

    Every member of a project reads its instances' jobs, and the field
    had no ``groups``: a stakeholder read a restore's password, decrypted,
    with ``read``. The flows that need it read it with ``sudo``, also
    when the job runs as a person without superuser rights.

    Post-install: its people need partners, and a module installed after
    this one may add a column to them that only its own defaults fill.
    """

    URL = "https://bob:hunter2@93.184.216.34/backup.zip"

    def setUp(self):
        """A project with a stakeholder and a developer, and an instance."""
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "secret-reader-host",
            "ip_address": "198.51.100.24",
            "user": "root",
            "wildcard_domain": "secret-reader.example",
        })
        project = self.env["cloud.project"].create({"name": "secret-reader"})
        self.instance = self.env["cloud.instance"].create({
            "name": "secret-reader-inst",
            "project_id": project.id,
            "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        self.stakeholder = new_test_user(
            self.env, login="secret-stakeholder",
            groups="base.group_user,incubacloud.group_cloud_user",
        )
        self.developer = new_test_user(
            self.env, login="secret-developer",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )
        project.member_ids = [(4, self.stakeholder.id), (4, self.developer.id)]
        self.secret = json.dumps({
            "machine": "93.184.216.34", "login": "bob", "password": "hunter2",
        })

    def _job(self, state=False):
        """A from-URL restore of the instance carrying the credential."""
        return self.env["cloud.job"].create({
            "name": "restore",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env.ref("incubacloud.restore_instance").id,
            "payload": {"mode": "from_url"},
            "secret_payload": self.secret,
            "state": state,
        })

    def test_a_project_member_cannot_read_a_jobs_secret(self):
        """They read the job, not its credential, however they ask."""
        job = self._job()
        for user in (self.stakeholder, self.developer):
            with self.subTest(user=user.login):
                theirs = job.with_user(user)
                self.assertEqual(theirs.read(["name"])[0]["name"], "restore")
                self.assertNotIn("secret_payload", set(theirs.fields_get()))
                with self.assertRaises(AccessError):
                    theirs.read(["secret_payload"])
                with self.assertRaises(AccessError):
                    theirs.search_read([("id", "=", job.id)], ["secret_payload"])
                with self.assertRaises(AccessError):
                    _value = theirs.secret_payload

    def test_a_developers_restore_from_a_link_still_logs_in(self):
        """The job runs as the developer who asked for it, without
        superuser rights, and still writes the credential for curl."""
        job_id = self.instance.with_user(self.developer).restore_from_url(self.URL)
        job = self.env["cloud.job"].browse(job_id)
        self.assertEqual(job.create_uid, self.developer)
        self.assertFalse(job.asked_as_superuser)
        executor = RestoreInstanceExecutor(
            job.with_user(self.developer), self.host,
        )
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        asyncio.run(executor.before_execute(transport))
        transport.upload_text_files.assert_awaited_once_with(
            {executor._curl_config_path(): 'user = "bob:hunter2"\n'},
            mode=0o600,
        )
        _label, command, _opts = executor._download_command(
            executor._remote_path(),
        )
        self.assertIn(f"--config {executor._curl_config_path()}", command)
        self.assertNotIn("hunter2", command)
        self.assertNotIn("hunter2", job.payload.get("url_display", ""))

    def test_a_user_name_with_a_colon_is_refused(self):
        """curl splits ``user`` at its first colon: the rest would be sent
        as the password."""
        from odoo.exceptions import UserError
        with self.assertRaisesRegex(UserError, "colon"):
            self.instance.restore_from_url(
                "sftp://a%3Ab:pw@93.184.216.34/backup.zip",
            )

    def test_a_developers_retry_keeps_the_credential(self):
        """The retry reads the failed job's secret as the server does."""
        retry_id = self._job(state="failed").with_user(self.developer).retry_job()
        self.assertEqual(
            self.env["cloud.job"].browse(retry_id).secret_payload, self.secret,
        )


class TestADownloadFromALinkRunsNothingItCarries(TransactionCase):
    """What a link carries reaches ``curl`` as words, never as shell.

    The address, the user and the password were written between single
    quotes into the command, unescaped: a quote in any of them ended the
    quoting, and whoever may ask for a restore from a link ran commands
    as the host's SSH account (28-oct-2026). And the password travelled
    in that command's argv, visible in ``ps`` while the download ran.
    """

    URL = "https://93.184.216.34/it's$(touch pwned)'`touch pwned`.zip"
    LOGIN = "o'brien$(touch pwned)"
    PASSWORD = "it's $(touch pwned) \"q\" \\x"

    def setUp(self):
        """A from-URL restore with a credential, its job directory here."""
        super().setUp()
        host = self.env["cloud.host"].create({
            "name": "quoted-link-host", "ip_address": "198.51.100.25",
            "user": "ubuntu", "wildcard_domain": "quoted-link.example",
        })
        project = self.env["cloud.project"].create({"name": "quoted-link"})
        instance = self.env["cloud.instance"].create({
            "name": "quoted-link-inst", "project_id": project.id,
            "host_id": host.id, "odoo_version": "19.0",
        })
        job = self.env["cloud.job"].create({
            "name": "restore",
            "host_id": host.id,
            "instance_id": instance.id,
            "job_type_id": self.env.ref("incubacloud.restore_instance").id,
            "payload": {
                "mode": "from_url", "url": self.URL,
                "resolve": "93.184.216.34:443:93.184.216.34",
            },
            "secret_payload": json.dumps({
                "machine": "93.184.216.34",
                "login": self.LOGIN,
                "password": self.PASSWORD,
            }),
        })
        self.executor = RestoreInstanceExecutor(job, host)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.executor._job_dir_path = f"{self.tmp}/job"
        Path(self.tmp, "job").mkdir()

    def _staged(self):
        """Run ``before_execute``; return what it staged owner-only."""
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        asyncio.run(self.executor.before_execute(transport))
        private = {}
        for upload in transport.upload_text_files.await_args_list:
            self.assertEqual(upload.kwargs.get("mode"), 0o600)
            private.update(upload.args[0])
        return private

    def test_lab_curl_gets_each_value_as_one_word(self):
        """The step runs on a real shell with ``curl`` recording its
        arguments: the address arrives whole and nothing else runs."""
        self._staged()
        curl = Path(self.tmp, "bin", "curl")
        curl.parent.mkdir()
        curl.write_text(
            '#!/bin/sh\nprintf \'%s\\0\' "$@" > "$ARGV"\n', encoding="utf-8",
        )
        curl.chmod(0o755)
        remote = self.executor._remote_path()
        _label, command, _opts = self.executor._download_command(remote)
        done = subprocess.run(
            ["bash", "-c", command], cwd=self.tmp, check=False,
            env={"PATH": f"{self.tmp}/bin:/usr/bin:/bin",
                 "ARGV": f"{self.tmp}/argv"},
        )
        self.assertEqual(done.returncode, 0)
        argv = Path(self.tmp, "argv").read_text(encoding="utf-8").split("\0")[:-1]
        self.assertEqual(argv, [
            "-q", "-fsS", "--max-time", "86400", "--globoff",
            "--noproxy", "*",
            "--proto", "=https,sftp,ftp", "--proto-redir", "=https",
            "--resolve", "93.184.216.34:443:93.184.216.34",
            "--config", self.executor._curl_config_path(),
            "-o", remote, self.URL,
        ])
        self.assertFalse(Path(self.tmp, "pwned").exists())
        self.assertEqual(shlex.split(command)[0], "curl")

    def test_lab_curl_reads_back_every_password(self):
        """The real ``curl`` reads the staged file back exactly, whatever
        the password holds: a space, a quote, a backslash, a ``#`` — which
        the netrc file it replaced quoted in a way curl before 7.84 (Ubuntu
        22.04's 7.81) does not read, so passwords that worked stopped
        working (28-oct-2026)."""
        import base64
        import http.server
        import threading

        heard = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            """Record the credential a request carries, answer 200."""

            def do_GET(self):
                """Record the ``Authorization`` header and answer empty."""
                heard.append(self.headers.get("Authorization", ""))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                """Keep the test's output clean."""

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/b.zip"
        for login, password in (
            ("bob", "hunter2"), ("bob", "two words"), ("bob", 'q"uote'),
            ("bob", "back\\slash"), ("bob", "#hash"), ("bob", "a#b"),
            ("bob", "tab\tnew\nline"), ("o'brien", "ünïcode:with:colons"),
            (self.LOGIN, self.PASSWORD),
        ):
            with self.subTest(password=password):
                heard.clear()
                config = Path(self.tmp, "curl.conf")
                config.write_text(
                    restore_instance_executor._curl_config(login, password),
                    encoding="utf-8",
                )
                done = subprocess.run(
                    ["curl", "-fsS", "--config", str(config), url],
                    capture_output=True, text=True, check=False, timeout=30,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                scheme, _sep, value = heard[0].partition(" ")
                self.assertEqual(scheme, "Basic")
                self.assertEqual(
                    base64.b64decode(value).decode(), f"{login}:{password}",
                )

    def test_the_credential_is_a_private_file_never_a_word(self):
        """The credential is staged 0600 in the job's directory, as a
        curl config file; the command names its path, and neither the
        user nor the password."""
        private = self._staged()
        self.assertEqual(list(private), [self.executor._curl_config_path()])
        self.assertTrue(
            self.executor._curl_config_path().startswith(f"{self.tmp}/job/"),
        )
        self.assertEqual(
            private[self.executor._curl_config_path()],
            'user = "o\'brien$(touch pwned):'
            'it\'s $(touch pwned) \\"q\\" \\\\x"\n',
        )
        _label, command, _opts = self.executor._download_command(
            self.executor._remote_path(),
        )
        self.assertNotIn("brien", command)
        self.assertNotIn("touch pwned) ", command)
        self.assertNotIn("password", command)


class TestALinkIsCheckedAgainWhereItIsFetched(TransactionCase):
    """The download step trusts no address or pin from its payload.

    ``restore_from_url`` checks a link and pins the address it resolves
    to; a restore queued any other way carries whatever its caller wrote.
    The executor checks the link again, computes the pin itself, and
    refuses the job before the host is told anything.
    """

    def setUp(self):
        """A host to restore on."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "checked-link-host", "ip_address": "198.51.100.26",
            "user": "root", "wildcard_domain": "checked-link.example",
        })
        self.project = self.env["cloud.project"].create({"name": "checked-link"})

    def _queued(self, url, resolve=PUBLIC_PIN):
        """Queue a restore from *url* the way any job is queued.

        One instance per job: a restore waiting in a queue keeps a second
        one out of it.

        :return: the executor that would run it
        """
        instance = self.env["cloud.instance"].create({
            "name": f"checked-link-{self.env['cloud.instance'].search_count([])}",
            "project_id": self.project.id, "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        job_id = self.env["cloud.job"].enqueue(
            self.host.id, instance.id, "restore_instance",
            payload={"mode": "from_url", "url": url, "resolve": resolve},
        )
        return RestoreInstanceExecutor(
            self.env["cloud.job"].browse(job_id), self.host,
        )

    def test_a_forbidden_link_queued_directly_runs_nothing(self):
        for url in FORBIDDEN_LINKS:
            with self.subTest(url=url):
                executor = self._queued(url)
                transport = MagicMock(spec=SSHTransport)
                with self.assertRaisesRegex(ValueError, "link"):
                    asyncio.run(executor.before_execute(transport))
                self.assertFalse(transport.method_calls)
                with self.assertRaisesRegex(ValueError, "link"):
                    executor.get_commands()

    def test_a_pin_in_the_payload_is_never_used(self):
        """The address curl dials is the one resolved here."""
        executor = self._queued(PUBLIC_LINK, resolve="93.184.216.34:443:10.0.0.1")
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        asyncio.run(executor.before_execute(transport))
        _label, command, _opts = executor._download_command(
            executor._remote_path(),
        )
        words = shlex.split(command)
        self.assertEqual(words[words.index("--resolve") + 1], PUBLIC_PIN)
        self.assertNotIn("10.0.0.1", command)

    def test_curl_keeps_to_the_link_s_schemes(self):
        """No other protocol, and no redirect to anything but https —
        none is followed anyway."""
        executor = self._queued(PUBLIC_LINK)
        _label, command, _opts = executor._download_command(
            executor._remote_path(),
        )
        words = shlex.split(command)
        self.assertEqual(words[words.index("--proto") + 1], "=https,sftp,ftp")
        self.assertEqual(words[words.index("--proto-redir") + 1], "=https")
        self.assertIn("--globoff", words)
        self.assertNotIn("-L", words)
        self.assertNotIn("--location", words)

    def test_curl_reads_nothing_the_host_configured(self):
        """No ``~/.curlrc`` (``-q`` works only as the first argument) and
        no proxy from the account's environment: either could send the
        download elsewhere than the pinned address. And no ``-C -``:
        each attempt downloads into a directory of its own, so there is
        never a partial file to resume."""
        executor = self._queued(PUBLIC_LINK)
        _label, command, _opts = executor._download_command(
            executor._remote_path(),
        )
        words = shlex.split(command)
        self.assertEqual(words[:2], ["curl", "-q"])
        self.assertEqual(words.count("-q"), 1)
        self.assertEqual(words[words.index("--noproxy") + 1], "*")
        self.assertLess(words.index("--noproxy"), words.index(PUBLIC_LINK))
        self.assertNotIn("-C", words)
        self.assertNotIn("--continue-at", words)

    def test_a_non_ascii_link_queued_directly_runs_nothing(self):
        for url in NON_ASCII_LINKS:
            with self.subTest(url=url):
                executor = self._queued(url)
                transport = MagicMock(spec=SSHTransport)
                with self.assertRaisesRegex(ValueError, "xn--"):
                    asyncio.run(executor.before_execute(transport))
                self.assertFalse(transport.method_calls)
                with self.assertRaisesRegex(ValueError, "xn--"):
                    executor.get_commands()

    def test_curl_dials_the_name_it_pins(self):
        """The link handed to curl is rebuilt from the name checked here,
        so ``--resolve`` matches it byte for byte and curl never looks
        the name up itself."""
        with _public_resolution():
            executor = self._queued("https://Files.Example.com/b.zip#part")
            _label, command, _opts = executor._download_command(
                executor._remote_path(),
            )
        words = shlex.split(command)
        pinned = words[words.index("--resolve") + 1]
        fetched = words[-1]
        self.assertEqual(fetched, "https://files.example.com/b.zip")
        self.assertEqual(pinned, "files.example.com:443:93.184.216.34")
        self.assertEqual(urlsplit(fetched).netloc, pinned.split(":")[0])


@tagged("-at_install", "post_install")
class TestPeopleReadGrantsAndNeverWriteThem(TransactionCase):
    """A grant's token names a directory on a host and its public key goes
    into ``authorized_keys``: only the panel writes them. A developer
    asks for a grant, checks it, revokes it and restores from it, and the
    panel writes the record each time.

    Post-install: its people need partners, and a module installed after
    this one may add a column to them that only its own defaults fill.
    """

    def setUp(self):
        """A developer of a project, an instance of it, and a grant."""
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "grant-writer-host", "ip_address": "198.51.100.27",
            "user": "root", "wildcard_domain": "grant-writer.example",
        })
        project = self.env["cloud.project"].create({"name": "grant-writer"})
        self.instance = self.env["cloud.instance"].create({
            "name": "grant-writer-inst", "project_id": project.id,
            "host_id": self.host.id, "odoo_version": "19.0",
        })
        self.developer = new_test_user(
            self.env, login="grant-developer",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )
        project.member_ids = [(4, self.developer.id)]
        self.mine = self.instance.with_user(self.developer)
        opened = self.mine.grant_restore_upload()
        self.grant = self.env["cloud.restore.upload.grant"].browse(
            opened["grant_id"],
        )
        self.env.flush_all()

    def test_a_developer_writes_no_grant(self):
        theirs = self.grant.with_user(self.developer)
        self.assertEqual(theirs.read(["token"])[0]["token"], self.grant.token)
        for values in ({"token": "0" * 16}, {"public_key": "ssh-ed25519 AAAA"},
                       {"received_sha256": "ab" * 32}):
            with self.subTest(values=values), self.assertRaises(AccessError):
                theirs.write(values)
        with self.assertRaises(AccessError):
            theirs.unlink()
        with self.assertRaises(AccessError):
            self.env["cloud.restore.upload.grant"].with_user(
                self.developer,
            ).create({
                "instance_id": self.instance.id, "host_id": self.host.id,
                "token": "0" * 16, "public_key": "ssh-ed25519 AAAA",
                "expires_at": fields.Datetime.now(),
            })

    def _job(self, code):
        """Return a job of type *code* on the grant, asked for by the
        developer, in their environment."""
        return self.env["cloud.job"].create({
            "name": code, "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env.ref(f"incubacloud.{code}").id,
            "payload": {"grant_id": self.grant.id},
        }).with_user(self.developer)

    def test_the_panel_still_writes_what_a_developer_s_jobs_report(self):
        digest = "ab" * 32
        verify = VerifyRestoreUploadExecutor(
            self._job("verify_restore_upload"), self.host,
        )
        asyncio.run(verify.on_success({"Inspect the upload": {
            "stdout": f"5 {digest} restore.zip\n", "exit_status": 0,
        }}))
        self.assertEqual(self.grant.received_sha256, digest)
        install = GrantRestoreUploadKeyExecutor(
            self._job("grant_restore_upload_key"), self.host,
        )
        asyncio.run(install.on_failure({}, ["refused"]))
        self.assertEqual(self.grant.state, "revoked")

    def test_a_developer_still_revokes_their_grant(self):
        self.mine.revoke_restore_upload(self.grant.id)
        self.assertEqual(self.grant.state, "revoked")


@tagged("-at_install", "post_install")
class TestAGrantIsReadOnlyByThoseWhoSeeItsInstance(TransactionCase):
    """A grant carries a token, a fingerprint and what arrived through it:
    whoever is scoped to projects reads the grants of those projects only,
    as for the instance's other records. Project managers and above see
    every instance, so they see every grant; the panel's own calls go
    through the instance and are not narrowed by the rule.
    """

    def setUp(self):
        """Two projects with an instance and a grant each, and their people."""
        super().setUp()
        self.registry_enter_test_mode()
        host = self.env["cloud.host"].create({
            "name": "grant-scope-host", "ip_address": "198.51.100.28",
            "user": "root", "wildcard_domain": "grant-scope.example",
        })
        self.users = {}
        for role in ("user", "consultant", "developer", "manager"):
            self.users[role] = new_test_user(
                self.env, login=f"grant-scope-{role}",
                groups=f"base.group_user,incubacloud.group_cloud_{role}",
            )
        self.instances = {}
        self.grants = {}
        for key in ("a", "b"):
            project = self.env["cloud.project"].create(
                {"name": f"grant-scope-{key}"},
            )
            self.instances[key] = self.env["cloud.instance"].create({
                "name": f"grant-scope-{key}", "project_id": project.id,
                "host_id": host.id, "odoo_version": "19.0",
            })
            self.grants[key] = self.env["cloud.restore.upload.grant"]._open(
                self.instances[key],
            )[0]
            if key == "a":
                project.member_ids = [
                    (4, self.users[r].id)
                    for r in ("user", "consultant", "developer")
                ]
        self.env.flush_all()

    def _grants_of(self, role):
        """Return the grants *role*'s user can search, among these two."""
        return self.env["cloud.restore.upload.grant"].with_user(
            self.users[role],
        ).search([("id", "in", [g.id for g in self.grants.values()])])

    def test_a_scoped_user_reads_the_grants_of_their_projects_only(self):
        for role in ("user", "consultant"):
            with self.subTest(role=role):
                self.assertEqual(self._grants_of(role), self.grants["a"])
                with self.assertRaises(AccessError):
                    self.grants["b"].with_user(self.users[role]).read(
                        ["token", "fingerprint", "received_filename"],
                    )

    def test_a_user_outside_every_project_reads_no_grant(self):
        outsider = new_test_user(
            self.env, login="grant-scope-outsider",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        found = self.env["cloud.restore.upload.grant"].with_user(
            outsider,
        ).search([("id", "in", [g.id for g in self.grants.values()])])
        self.assertFalse(found)

    def test_project_managers_and_above_read_every_grant(self):
        everything = self.grants["a"] | self.grants["b"]
        for role in ("developer", "manager"):
            with self.subTest(role=role):
                found = self._grants_of(role)
                self.assertEqual(found, everything)
                found.read(["token"])

    def test_a_developer_requests_checks_and_revokes_their_grant(self):
        mine = self.instances["a"].with_user(self.users["developer"])
        opened = mine.grant_restore_upload()
        self.assertTrue(opened["fingerprint"])
        grant = self.env["cloud.restore.upload.grant"].browse(
            opened["grant_id"],
        )
        self.assertTrue(mine.verify_restore_upload(grant.id))
        mine.revoke_restore_upload(grant.id)
        self.assertEqual(grant.state, "revoked")
