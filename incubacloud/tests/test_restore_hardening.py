"""What a restore from outside the panel may take, and who sees its link.

A security review of the restore tools found, in the code they share with
the panel's Import dialog:

* the link of a restore — a presigned link's signature, a share link's
  token — in the job's payload, which every member of the project reads;
* no bound on what a restore writes on its host's disk, which its other
  instances share;
* an agent's link free to name any port, and any server of the panel's;
* a key that opened a door opening another once its grant was purged,
  and two doors of one instance at once;
* the link, in the download's command line, shown by ``ps`` to every
  account of the host.

These tests pin each answer, attack by attack.
"""
import asyncio
import contextlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import psycopg2

from odoo import fields
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.incubacloud.models import cloud_restore_upload_grant
from odoo.addons.incubacloud.models.restore_instance_executor import (
    RestoreInstanceExecutor,
)
from odoo.addons.incubacloud.models.restore_upload_key_executors import (
    VerifyRestoreUploadExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.net import restore_source
from odoo.addons.incubacloud.net.outbound import OutboundError
from odoo.addons.incubacloud.net.restore_source import (
    cut,
    curl_url,
    masked,
    shown_link,
    told_link,
    validate,
)
from odoo.addons.incubacloud.restore_space import (
    GIB,
    RESERVE_BYTES,
    UNPACK_FACTOR,
    largest_download,
    room_needed,
)

#: A presigned link: its query downloads the archive by itself.
SIGNATURE = "deadbeefcafe0123456789abcdef0123456789abcdef0123456789abcdef0123"
SIGNED = (
    "https://93.184.216.34/bucket/tenant-7/backup.zip"
    "?X-Amz-Credential=AKIAEXAMPLE20261104&X-Amz-Signature=" + SIGNATURE
)
#: A digest the upload check reports.
SHA = "ab" * 32


def _resolving_to(address="93.184.216.34"):
    """Have every name resolve to *address*, without DNS.

    :return: the patcher, to be used as a context manager
    """
    return patch.object(
        restore_source, "_resolve_public", autospec=True, return_value=address,
    )


def _host_answers(free=100 * GIB, size=None, others=None):
    """Return a ``transport.run`` that answers as a host with *free* bytes free.

    :param int free: what ``df`` prints, or None for nothing
    :param int size: what ``stat`` prints, or None for nothing
    :param dict others: ``{command prefix: stdout}`` for other commands
    :rtype: callable
    """
    def run(command, *_args, **_kwargs):
        """Answer *command* as the host would."""
        if command.startswith("df "):
            return CommandResult("" if free is None else f"{free}\n", 0)
        if command.startswith("stat "):
            return CommandResult("" if size is None else f"{size}\n", 0)
        for prefix, stdout in (others or {}).items():
            if command.startswith(prefix):
                return CommandResult(stdout, 0)
        return CommandResult("", 0)
    return run


def _transport(free=100 * GIB, size=None, others=None):
    """Return a transport mock whose host has *free* bytes free.

    :rtype: MagicMock
    """
    transport = MagicMock(spec=SSHTransport)
    transport.run.side_effect = _host_answers(free, size, others)
    return transport


class RestoreFixture:
    """A host, a project with a staging and a production, and its people."""

    def _restore_fixture(self, ip="198.51.100.40"):
        """Create the host, the instances and the people of the project."""
        slug = ip.replace(".", "-")
        self.host = self.env["cloud.host"].create({
            "name": f"hardening-host-{slug}",
            "ip_address": ip,
            "user": "root",
            "wildcard_domain": "hardening.example",
        })
        self.project = self.env["cloud.project"].create({"name": f"hardening-{slug}"})
        self.instance = self.env["cloud.instance"].create({
            "name": f"hardening-inst-{slug}",
            "project_id": self.project.id,
            "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        self.stakeholder = new_test_user(
            self.env, login=f"hardening-stakeholder-{slug}",
            groups="base.group_user,incubacloud.group_cloud_user",
        )
        self.developer = new_test_user(
            self.env, login=f"hardening-developer-{slug}",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )
        self.other_developer = new_test_user(
            self.env, login=f"hardening-other-{slug}",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )
        self.project.member_ids = [
            (4, self.stakeholder.id), (4, self.developer.id),
            (4, self.other_developer.id),
        ]

    def _instance(self, name, **vals):
        """Another instance of the project, on the same host."""
        return self.env["cloud.instance"].create({
            "name": name,
            "project_id": self.project.id,
            "host_id": self.host.id,
            "odoo_version": "19.0",
        } | vals)


class TestTheLinksForms(BaseCase):
    """The link as it is stored, reported and approved: never whole."""

    def test_a_piece_keeps_its_ends_and_never_enough_to_download(self):
        """Short pieces whole; longer ones at most 4 characters at each end,
        never fewer than 8 left out; an extension kept."""
        for piece, shown in (
            ("s", "s"),
            ("download", "download"),
            ("backup.zip", "backup.zip"),
            ("123456789", "…"),
            ("t-AbCdEfGhIjKl", "t-A…jKl"),
            (SIGNATURE, "dead…0123"),
            ("backup-2026-11-04.zip", "back…1-04.zip"),
        ):
            with self.subTest(piece=piece):
                self.assertEqual(cut(piece), shown)
        for length in range(9, 40):
            piece = "x" * length
            with self.subTest(length=length):
                self.assertGreaterEqual(length - (len(cut(piece)) - 1), 8)

    def test_a_token_as_the_last_segment_is_not_kept_whole(self):
        """``we.tl/t-…`` and ``/s/<token>``: the audit and the answer keep
        the file's place, not the token."""
        for url, token in (
            ("https://we.tl/t-AbCdEfGhIjKl", "t-AbCdEfGhIjKl"),
            ("https://cloud.example.com/s/Z8k2LmQ9pR4tW7xY", "Z8k2LmQ9pR4tW7xY"),
        ):
            with self.subTest(url=url):
                self.assertNotIn(token, shown_link(url))
        self.assertEqual(
            shown_link("https://u:p@[2001:db8::1]:8443/s/tok/d.zip?dl=1"),
            "https://[2001:db8::1]:8443/…/d.zip?…",
        )

    def test_two_look_alike_links_read_differently_where_they_are_approved(self):
        """Same host, same file's name: the user's link and an attacker's
        read the same in ``shown_link``, not in ``told_link``."""
        pairs = (
            (
                "https://s3.eu-west-1.amazonaws.com/acme-backups/backup.zip?X-Amz-Signature=" + SIGNATURE,
                "https://s3.eu-west-1.amazonaws.com/evil-bucket/backup.zip?X-Amz-Signature=" + SIGNATURE,
            ),
            (
                "https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOpQrStUvWxYz&export=download",
                "https://drive.google.com/uc?id=1ZzZzEvilEvilEvilEvilEvilQq&export=download",
            ),
            (
                "https://www.dropbox.com/scl/fi/abcdef123456ghij/backup.zip?rlkey=zyxwvut987654&dl=1",
                "https://www.dropbox.com/scl/fi/qrstuv987654wxyz/backup.zip?rlkey=zyxwvut987654&dl=1",
            ),
        )
        for mine, theirs in pairs:
            with self.subTest(mine=mine):
                self.assertEqual(shown_link(mine), shown_link(theirs))
                self.assertNotEqual(told_link(mine), told_link(theirs))
                for url in (mine, theirs):
                    query = urlsplit(url).query
                    for value in (part.partition("=")[2] for part in query.split("&")):
                        if len(value) > 8:
                            self.assertNotIn(value, told_link(url))

    def test_an_agent_link_keeps_to_port_443_and_off_the_panel_s_servers(self):
        """``only_port`` and ``refused``: what the check of public addresses
        lets through."""
        with _resolving_to():
            with self.assertRaisesRegex(OutboundError, "port 443"):
                validate("https://files.example.com:8443/b.zip", only_port=443)
            validate("https://files.example.com/b.zip", only_port=443)
            import ipaddress
            with self.assertRaisesRegex(OutboundError, "server of this panel"):
                validate(
                    "https://files.example.com/b.zip",
                    refused=[ipaddress.ip_network("93.184.216.0/24")],
                )
        with self.assertRaisesRegex(OutboundError, "impossible port"):
            validate("https://files.example.com:99999/b.zip")


@tagged("-at_install", "post_install")
class TestTheLinkLeavesThePayload(RestoreFixture, TransactionCase):
    """Every member of a project reads its instances' jobs, payload and all:
    the link a restore downloads is kept encrypted, the server's alone.

    Post-install: its people need partners, and a module installed after
    this one may add a column to them that only its own defaults fill.
    """

    def setUp(self):
        """The fixture, and a production with a backup destination."""
        super().setUp()
        self.registry_enter_test_mode()
        self._restore_fixture()
        self.production = self._instance(
            "hardening-prod", environment="production",
            custom_backup_dst="s3://hardening-bucket/prod",
        )

    def test_a_stakeholder_reads_no_signature_through_the_orm(self):
        """``search_read`` and ``read`` of the payload, as any member: no
        signature, no credential; ``shown_link`` only."""
        with _resolving_to():
            job_id = self.instance.with_user(self.developer).restore_from_url(SIGNED)
        for user in (self.stakeholder, self.developer):
            with self.subTest(user=user.login):
                Jobs = self.env["cloud.job"].with_user(user)
                rows = Jobs.search_read([("id", "=", job_id)], ["payload"])
                rows += Jobs.browse(job_id).read(["payload", "name"])
                text = json.dumps(rows)
                self.assertTrue(rows)
                for hidden in (SIGNATURE, "AKIAEXAMPLE20261104", "tenant-7"):
                    self.assertNotIn(hidden, text)
                payload = rows[0]["payload"]
                self.assertNotIn("url", payload)
                self.assertEqual(payload["url_display"], shown_link(SIGNED))
        self.assertEqual(
            self.env["cloud.job"].browse(job_id)._restore_url_link(), SIGNED,
        )

    def test_a_safety_backup_first_leaves_the_link_on_the_restore_step(self):
        """Production takes its backup first: the link and the credential
        are the restore's, not the backup's that the call answers."""
        with _resolving_to():
            first_id = self.production.with_user(self.developer).restore_from_url(
                "https://bob:hunter2@93.184.216.34/backup.zip",
            )
        Jobs = self.env["cloud.job"].sudo()
        first = Jobs.browse(first_id)
        self.assertEqual(first.job_type_id.code, "backup_create")
        self.assertFalse(first.secret_payload)
        restore = Jobs.search([
            ("queue_chain_key", "=", first.queue_chain_key),
            ("job_type_id.code", "=", "restore_instance"),
        ])
        self.assertEqual(len(restore), 1)
        self.assertEqual(
            restore._restore_url_link(), "https://93.184.216.34/backup.zip",
        )
        self.assertEqual(
            restore._restore_url_credential(), ("93.184.216.34", "bob", "hunter2"),
        )

    def test_a_retry_downloads_the_same_link(self):
        """The retry takes the failed job's secret, link included."""
        with _resolving_to():
            job_id = self.instance.with_user(self.developer).restore_from_url(SIGNED)
        job = self.env["cloud.job"].browse(job_id)
        job.sudo().with_context(ic_skip_state_hook=True).write({"state": "failed"})
        retry = self.env["cloud.job"].browse(
            job.with_user(self.developer).retry_job(),
        )
        self.assertNotIn("url", retry.payload)
        self.assertEqual(retry._restore_url_link(), SIGNED)
        with _resolving_to():
            url, _pin = RestoreInstanceExecutor(retry, self.host)._checked_link()
        self.assertEqual(url, curl_url(urlsplit(SIGNED)))

    def test_a_job_queued_with_the_link_in_its_payload_still_downloads_it(self):
        """A job the upgrade did not reach: its payload's link is read."""
        job = self.env["cloud.job"].create({
            "name": "restore",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env.ref("incubacloud.restore_instance").id,
            "payload": {"mode": "from_url", "url": SIGNED},
        })
        with _resolving_to():
            url, _pin = RestoreInstanceExecutor(job, self.host)._checked_link()
        self.assertEqual(url, curl_url(urlsplit(SIGNED)))


class TestTheLinkIsNeverAWordOfTheCommand(RestoreFixture, TransactionCase):
    """``ps`` shows a command line to every account of the host: the link
    goes in the curl config file staged owner-only, as the credential."""

    def setUp(self):
        """A restore from a presigned link, its job directory here."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "argv-host", "ip_address": "198.51.100.41",
            "user": "ubuntu", "wildcard_domain": "argv.example",
        })
        project = self.env["cloud.project"].create({"name": "argv"})
        self.instance = self.env["cloud.instance"].create({
            "name": "argv-inst", "project_id": project.id,
            "host_id": self.host.id, "odoo_version": "19.0",
        })
        with _resolving_to():
            job_id = self.instance.restore_from_url(SIGNED)
        self.executor = RestoreInstanceExecutor(
            self.env["cloud.job"].browse(job_id), self.host,
        )
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.executor._job_dir_path = f"{self.tmp}/job"
        Path(self.tmp, "job").mkdir()

    def _staged(self, free=100 * GIB):
        """Run ``before_execute``; return what it staged owner-only."""
        transport = _transport(free)
        asyncio.run(self.executor.before_execute(transport))
        private = {}
        for upload in transport.upload_text_files.await_args_list:
            self.assertEqual(upload.kwargs.get("mode"), 0o600)
            private.update(upload.args[0])
        return private

    def test_the_link_is_in_the_private_config_and_not_in_the_command(self):
        """The config names it whole; the command, nothing of its query."""
        private = self._staged()
        config = private[self.executor._curl_config_path()]
        self.assertIn(f'url = "{curl_url(urlsplit(SIGNED))}"\n', config)
        self.assertNotIn("user =", config)
        _label, command, _opts = self.executor._download_command(
            self.executor._remote_path(),
        )
        for hidden in (SIGNATURE, "X-Amz", "tenant-7", "backup.zip"):
            self.assertNotIn(hidden, command)
        self.assertIn(f"--config {self.executor._curl_config_path()}", command)
        self.assertIn(f"rm -f {self.executor._curl_config_path()}", command)

    def test_lab_the_argv_curl_gets_holds_no_link(self):
        """On a real shell, with ``curl`` recording its arguments."""
        self._staged()
        curl = Path(self.tmp, "bin", "curl")
        curl.parent.mkdir()
        curl.write_text(
            '#!/bin/sh\nprintf \'%s\\0\' "$@" > "$ARGV"\n', encoding="utf-8",
        )
        curl.chmod(0o755)
        _label, command, _opts = self.executor._download_command(
            self.executor._remote_path(),
        )
        done = subprocess.run(
            ["bash", "-c", command], cwd=self.tmp, check=False,
            env={"PATH": f"{self.tmp}/bin:/usr/bin:/bin", "ARGV": f"{self.tmp}/argv"},
        )
        self.assertEqual(done.returncode, 0)
        argv = Path(self.tmp, "argv").read_text(encoding="utf-8")
        self.assertIn(self.executor._curl_config_path(), argv)
        self.assertNotIn(SIGNATURE, argv)
        self.assertNotIn("93.184.216.34/bucket", argv)

    def test_lab_curl_downloads_the_link_its_config_names(self):
        """The real ``curl`` reads ``url`` from the config: the query reaches
        the server whole, and the archive lands where ``-o`` says."""
        import http.server
        import threading

        heard = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            """Record the path asked for, answer a small body."""

            def do_GET(self):
                """Record the path and answer ``zip``."""
                heard.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "3")
                self.end_headers()
                self.wfile.write(b"zip")

            def log_message(self, *_args):
                """Keep the test's output clean."""

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = (
            f"http://127.0.0.1:{server.server_port}/b/backup.zip"
            f'?X-Amz-Signature={SIGNATURE}&q="quoted"'
        )
        from odoo.addons.incubacloud.models import restore_instance_executor as rie
        config = Path(self.tmp, "curl.conf")
        config.write_text(rie._curl_url_config(url), encoding="utf-8")
        out = Path(self.tmp, "out.zip")
        done = subprocess.run(
            ["curl", "-q", "-fsS", "--globoff", "--config", str(config), "-o", str(out)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(out.read_bytes(), b"zip")
        self.assertEqual(heard, [urlsplit(url).path + "?" + urlsplit(url).query])


class TestARestoreTakesNoMoreDiskThanIsFree(RestoreFixture, TransactionCase):
    """An archive larger than the disk free, or a server that never stops
    sending, filled the host's disk and stopped every instance on it."""

    def setUp(self):
        """A restore from a link, not yet measured."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "disk-host", "ip_address": "198.51.100.42",
            "user": "root", "wildcard_domain": "disk.example",
        })
        self.project = self.env["cloud.project"].create({"name": "disk"})
        self.instance = self.env["cloud.instance"].create({
            "name": "disk-inst", "project_id": self.project.id,
            "host_id": self.host.id, "odoo_version": "19.0",
        })
        with _resolving_to():
            job_id = self.instance.restore_from_url(SIGNED)
        self.executor = RestoreInstanceExecutor(
            self.env["cloud.job"].browse(job_id), self.host,
        )

    def test_the_thresholds_say_what_a_restore_needs(self):
        """The reserve stays free; the archive shares the rest with its
        unpacking."""
        self.assertEqual(largest_download(RESERVE_BYTES), 0)
        self.assertEqual(largest_download(RESERVE_BYTES - 1), 0)
        self.assertEqual(
            largest_download(RESERVE_BYTES + 3 * GIB), 3 * GIB // (1 + UNPACK_FACTOR),
        )
        self.assertEqual(room_needed(GIB, copies=1), RESERVE_BYTES + 3 * GIB)

    def test_a_disk_with_only_the_reserve_free_downloads_nothing(self):
        transport = _transport(free=4 * GIB)
        with _resolving_to(), self.assertRaisesRegex(RuntimeError, "Not enough free disk"):
            asyncio.run(self.executor.before_execute(transport))
        transport.upload_text_files.assert_not_awaited()

    def test_a_disk_that_cannot_be_measured_downloads_nothing(self):
        transport = _transport(free=None)
        with _resolving_to(), self.assertRaisesRegex(RuntimeError, "measured"):
            asyncio.run(self.executor.before_execute(transport))
        transport.upload_text_files.assert_not_awaited()
        with _resolving_to(), self.assertRaisesRegex(ValueError, "measured"):
            self.executor._download_command(self.executor._remote_path())

    def test_a_download_writes_no_more_than_the_disk_free_leaves(self):
        """curl's ``--max-filesize`` and the shell's ``ulimit -f`` (KiB)."""
        free = RESERVE_BYTES + 30 * GIB
        with _resolving_to():
            asyncio.run(self.executor.before_execute(_transport(free=free)))
            _label, command, _opts = self.executor._download_command(
                self.executor._remote_path(),
            )
        limit = 10 * GIB
        self.assertEqual(self.executor._download_limit, limit)
        words = shlex.split(command)
        self.assertEqual(words[words.index("--max-filesize") + 1], str(limit))
        self.assertEqual(words[:2], ["bash", "-c"])
        self.assertIn('ulimit -f "$1"', words[2])
        self.assertIn('trap "" XFSZ', words[2])
        self.assertEqual(words[4], str(limit // 1024 + 1))
        self.assertEqual(words[words.index("curl") + 1], "-q")

    def test_lab_a_server_that_never_stops_sending_stops_at_the_limit(self):
        """On a real bash, ``curl`` standing in as a writer of endless
        zeros: the write past the limit fails (curl's 23), the file stays
        within it, and a smaller archive goes through."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        curl = Path(tmp, "bin", "curl")
        curl.parent.mkdir()
        curl.write_text(
            "#!/bin/sh\n"
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            'head -c "$SEND" /dev/zero > "$out" || exit 23\n',
            encoding="utf-8",
        )
        curl.chmod(0o755)
        self.executor._job_dir_path = f"{tmp}/job"
        Path(tmp, "job").mkdir()
        self.executor._download_limit = 64 * 1024
        remote = self.executor._remote_path()
        with _resolving_to():
            _label, command, _opts = self.executor._download_command(remote)
        for send, status in ((10 * 1024 * 1024, 23), (10 * 1024, 0)):
            with self.subTest(send=send):
                done = subprocess.run(
                    ["bash", "-c", command], cwd=tmp, check=False,
                    capture_output=True,
                    env={"PATH": f"{tmp}/bin:/usr/bin:/bin", "SEND": str(send)},
                )
                self.assertEqual(done.returncode, status, done.stderr)
                self.assertLessEqual(os.path.getsize(remote), 64 * 1024 + 1024)
        errors = self.executor.parse_results(
            {"Download the archive": {"exit_status": 63, "stdout": ""}},
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("largest archive", errors[0])

    def test_an_archive_on_the_host_is_restored_only_with_room_for_it(self):
        """An upload is copied out of its door, then unpacked: refused
        before the copy when the disk cannot take both."""
        size = 10 * GIB
        for copies, free, refused in (
            (1, room_needed(size, 1) - 1, True),
            (1, room_needed(size, 1), False),
            (0, room_needed(size, 0), False),
            (0, room_needed(size, 0) - 1, True),
        ):
            with self.subTest(copies=copies, free=free):
                check = self.executor._check_room(
                    _transport(free=free, size=size), "/tmp/a.zip", copies,
                )
                if refused:
                    with self.assertRaisesRegex(RuntimeError, "Not enough free disk"):
                        asyncio.run(check)
                else:
                    asyncio.run(check)
        with self.assertRaisesRegex(RuntimeError, "size"):
            asyncio.run(self.executor._check_room(
                _transport(size=None), "/tmp/a.zip", 0,
            ))

    def test_an_rsync_archive_too_large_for_the_disk_is_not_restored(self):
        job = self.env["cloud.job"].create({
            "name": "restore", "host_id": self.host.id,
            "instance_id": self._instance("disk-rsync").id,
            "job_type_id": self.env.ref("incubacloud.restore_instance").id,
            "payload": {"mode": "rsync"},
        })
        executor = RestoreInstanceExecutor(job, self.host)
        transport = _transport(free=room_needed(GIB) - 1, size=GIB)
        with self.assertRaisesRegex(RuntimeError, "Not enough free disk"):
            asyncio.run(executor.before_execute(transport))


class TestACheckOffersNothingTheDiskCannotRestore(TransactionCase):
    """What came through a door is offered for a restore only when the
    server's disk can take its copy and its unpacking."""

    def setUp(self):
        """A door whose check is about to report."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "check-host", "ip_address": "198.51.100.43",
            "user": "root", "wildcard_domain": "check.example",
        })
        project = self.env["cloud.project"].create({"name": "check"})
        instance = self.env["cloud.instance"].create({
            "name": "check-inst", "project_id": project.id,
            "host_id": self.host.id, "odoo_version": "19.0",
        })
        self.grant, _key = self.env["cloud.restore.upload.grant"]._open(instance)
        job = self.env["cloud.job"].create({
            "name": "check", "host_id": self.host.id, "instance_id": instance.id,
            "job_type_id": self.env.ref("incubacloud.verify_restore_upload").id,
            "payload": {"grant_id": self.grant.id},
        })
        self.executor = VerifyRestoreUploadExecutor(job, self.host)

    def _report(self, size, free):
        """Run the check's end on what the host printed."""
        results = {
            "Inspect the upload": {"stdout": f"{size} {SHA} restore.zip\n"},
            "Measure free disk": {"stdout": "" if free is None else f"{free}\n"},
        }
        asyncio.run(self.executor.on_success(results))

    def test_the_check_measures_the_disk_of_the_door(self):
        labels = [command[0] for command in self.executor.get_commands()]
        self.assertEqual(labels, ["Inspect the upload", "Measure free disk"])
        self.assertIn(self.grant._directory(), self.executor.get_commands()[1][1])

    def test_an_upload_the_disk_cannot_restore_is_not_offered(self):
        size = 4 * GIB
        for free in (room_needed(size, copies=1) - 1, None):
            with self.subTest(free=free):
                self.grant.write({
                    "received_filename": "old.zip", "received_sha256": "cd" * 32,
                })
                self._report(size, free)
                self.assertFalse(self.grant.received_sha256)
                self.assertFalse(self.grant.received_filename)

    def test_an_upload_the_disk_can_restore_is_offered(self):
        size = 4 * GIB
        self._report(size, room_needed(size, copies=1))
        self.assertEqual(
            (self.grant.received_filename, self.grant.received_sha256),
            ("restore.zip", SHA),
        )


@tagged("-at_install", "post_install")
class TestOneDoorAtATime(RestoreFixture, TransactionCase):
    """Each door is a directory anybody holding its key fills, on the disk
    the host's instances share: one per instance at a time."""

    def setUp(self):
        super().setUp()
        self._restore_fixture("198.51.100.44")
        self.Grant = self.env["cloud.restore.upload.grant"]

    def _doors(self, state="granted"):
        """The instance's doors in *state*."""
        return self.Grant.sudo().search([
            ("instance_id", "=", self.instance.id), ("state", "=", state),
        ])

    def test_another_person_s_open_door_refuses_a_new_one(self):
        self.instance.with_user(self.developer).grant_restore_upload()
        with self.assertRaisesRegex(UserError, "already open"):
            self.instance.with_user(self.other_developer).grant_restore_upload()
        self.assertEqual(len(self._doors()), 1)

    def test_the_caller_s_own_open_door_is_closed_first(self):
        """A key lost with the dialog that showed it: the next one opens,
        and the first is closed, its revocation queued."""
        mine = self.instance.with_user(self.developer)
        first = self.Grant.browse(mine.grant_restore_upload()["grant_id"])
        second = self.Grant.browse(mine.grant_restore_upload()["grant_id"])
        self.assertEqual(first.sudo().state, "revoked")
        self.assertEqual(self._doors(), second.sudo())
        revocations = self.env["cloud.job"].sudo().search([
            ("job_type_id.code", "=", "revoke_restore_upload_key"),
            ("instance_id", "=", self.instance.id),
        ])
        self.assertEqual(
            [job.payload["grant_id"] for job in revocations], [first.id],
        )

    def test_an_expired_door_is_closed_before_another_opens(self):
        theirs = self.Grant.browse(
            self.instance.with_user(self.other_developer).grant_restore_upload()[
                "grant_id"
            ],
        )
        theirs.sudo().expires_at = fields.Datetime.now() - timedelta(minutes=1)
        self.instance.with_user(self.developer).grant_restore_upload()
        self.assertEqual(theirs.sudo().state, "revoked")
        self.assertEqual(len(self._doors()), 1)

    def test_a_door_another_transaction_opened_meanwhile_refuses_a_new_one(self):
        """The fresh read sees a door committed after this snapshot."""
        ghost = (self.Grant.sudo().search([], order="id desc", limit=1).id or 0) + 1000

        @contextlib.contextmanager
        def fresh(_registry):
            """A READ COMMITTED cursor that sees the other transaction's door."""
            cursor = MagicMock(spec=psycopg2.extensions.cursor)
            cursor.fetchall.return_value = [(ghost,)]
            yield cursor

        with (
            patch.object(cloud_restore_upload_grant, "read_committed_cursor", fresh),
            self.assertRaisesRegex(UserError, "a moment ago"),
        ):
            self.instance.with_user(self.developer).grant_restore_upload()
        self.assertFalse(self._doors())

    def test_another_instance_s_door_does_not_count(self):
        self.instance.with_user(self.developer).grant_restore_upload()
        other = self._instance("hardening-other-inst")
        other.with_user(self.other_developer).grant_restore_upload()
        self.assertEqual(len(self._doors()), 1)


class TestAKeyOpensOneDoorEver(TransactionCase):
    """A key that opened a door never opens another: not after its grant
    is purged, nor when two doors are asked for at once."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "key-host", "ip_address": "198.51.100.45",
            "user": "root", "wildcard_domain": "key.example",
        })
        self.project = self.env["cloud.project"].create({"name": "key"})
        self.instances = self.env["cloud.instance"].create([
            {
                "name": f"key-inst-{n}", "project_id": self.project.id,
                "host_id": self.host.id, "odoo_version": "19.0",
            }
            for n in range(3)
        ])
        self.Grant = self.env["cloud.restore.upload.grant"]
        self.Key = self.env["cloud.restore.upload.key"]

    def _public_key(self):
        """A new ed25519 public key line."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        return ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode()

    def test_a_key_used_once_is_refused_after_its_grant_is_purged(self):
        public = self._public_key()
        grant, _none = self.Grant._open(self.instances[0], public_key=public)
        grant._revoke()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_restore_upload_grant SET write_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=31), grant.id),
        )
        self.Grant.invalidate_model()
        self.Grant._gc_grants()
        self.assertFalse(grant.exists())
        with self.assertRaisesRegex(UserError, "already opened"):
            self.Grant._open(self.instances[1], public_key=public)

    def test_two_doors_asked_for_at_once_with_one_key_open_one(self):
        """The other transaction's grant is not in this snapshot; its key
        is in the database's unique index, and wins."""
        public = self._public_key()
        self.Key.sudo().create({"fingerprint": self.Grant._fingerprint(public)})
        before = self.Grant.search_count([])
        with self.assertRaisesRegex(UserError, "already opened"):
            self.Grant._open(self.instances[2], public_key=public)
        # Refused before any grant was made; the transaction goes on.
        self.assertEqual(self.Grant.search_count([]), before)

    def test_the_database_keeps_each_fingerprint_once(self):
        self.Key.sudo().create({"fingerprint": "SHA256:unique-test"})
        self.env.flush_all()
        with (
            mute_logger("odoo.sql_db"),
            self.assertRaises(psycopg2.errors.UniqueViolation),
            self.env.cr.savepoint(),
        ):
            self.env.cr.execute(
                "INSERT INTO cloud_restore_upload_key (fingerprint)"
                " VALUES ('SHA256:unique-test')",
            )

    def test_every_key_that_opened_a_door_is_remembered(self):
        grant, _private = self.Grant._open(self.instances[0])
        public = self._public_key()
        mine, _none = self.Grant._open(self.instances[1], public_key=public)
        self.assertEqual(
            set(self.Key.sudo().search([]).mapped("fingerprint"))
            & {grant.fingerprint, mine.fingerprint},
            {grant.fingerprint, mine.fingerprint},
        )


class TestTheUpgradeMovesTheLinks(TransactionCase):
    """1.0.212's post-migrate: the jobs stored before it lose the link from
    their payload; those that may still run keep it encrypted."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "upgrade-host", "ip_address": "198.51.100.46",
            "user": "root", "wildcard_domain": "upgrade.example",
        })
        project = self.env["cloud.project"].create({"name": "upgrade"})
        self.project = project
        self.migration = self._migration()

    def _migration(self):
        """Return the 1.0.212 post-migrate, loaded as Odoo loads it."""
        path = os.path.join(
            get_module_path("incubacloud"), "migrations", "1.0.212",
            "post-migrate.py",
        )
        spec = importlib.util.spec_from_file_location(
            "ic_post_migrate_1_0_212", path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _old_job(self, state, secret=None):
        """A restore from a link as 1.0.211 stored it, in *state*."""
        instance = self.env["cloud.instance"].create({
            "name": f"upgrade-{state}-{self.env['cloud.instance'].search_count([])}",
            "project_id": self.project.id, "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        return self.env["cloud.job"].create({
            "name": "restore",
            "host_id": self.host.id,
            "instance_id": instance.id,
            "job_type_id": self.env.ref("incubacloud.restore_instance").id,
            "payload": {
                "mode": "from_url", "url": SIGNED, "url_display": masked(SIGNED),
                "resolve": "93.184.216.34:443:93.184.216.34", "neutralize": True,
            },
            "secret_payload": secret,
            "state": state,
        })

    def test_a_job_that_may_run_keeps_its_link_encrypted(self):
        credential = {"machine": "93.184.216.34", "login": "bob", "password": "pw"}
        jobs = {
            state: self._old_job(state, json.dumps(credential) if state == "queued" else None)
            for state in ("queued", "failed", "done", "cancelled")
        }
        self.env.flush_all()
        self.migration.migrate(self.env.cr, "1.0.211")
        self.env.invalidate_all()
        for state, job in jobs.items():
            with self.subTest(state=state):
                self.assertNotIn("url", job.payload)
                self.assertNotIn(SIGNATURE, json.dumps(job.payload))
                self.assertEqual(job.payload["url_display"], shown_link(SIGNED))
                self.assertTrue(job.payload["neutralize"])
                secret = job._restore_url_secret()
                if state in ("queued", "failed"):
                    self.assertEqual(secret["url"], SIGNED)
                else:
                    self.assertNotIn("url", secret)
        self.assertEqual(
            jobs["queued"]._restore_url_credential(), ("93.184.216.34", "bob", "pw"),
        )
        with _resolving_to():
            url, _pin = RestoreInstanceExecutor(
                jobs["queued"], self.host,
            )._checked_link()
        self.assertEqual(url, curl_url(urlsplit(SIGNED)))
        # Run again: nothing left to move.
        self.assertEqual(self.migration._move_restore_links(self.env), 0)

    def test_the_upgrade_remembers_the_keys_of_the_doors_stored(self):
        instance = self.env["cloud.instance"].create({
            "name": "upgrade-door", "project_id": self.project.id,
            "host_id": self.host.id, "odoo_version": "19.0",
        })
        grant, _private = self.env["cloud.restore.upload.grant"]._open(instance)
        Key = self.env["cloud.restore.upload.key"].sudo()
        Key.search([("fingerprint", "=", grant.fingerprint)]).unlink()
        self.env.flush_all()
        self.migration.migrate(self.env.cr, "1.0.211")
        self.migration.migrate(self.env.cr, "1.0.211")
        self.assertEqual(Key.search_count([("fingerprint", "=", grant.fingerprint)]), 1)

    def test_a_fresh_install_changes_nothing(self):
        job = self._old_job("queued")
        self.env.flush_all()
        self.migration.migrate(self.env.cr, None)
        self.env.invalidate_all()
        self.assertEqual(job.payload["url"], SIGNED)


@tagged("-at_install", "post_install")
class TestAnAgentsLinkRules(RestoreFixture, TransactionCase):
    """An agent's link: https on 443, and none of the panel's own servers,
    here and again where it is fetched. The panel's own Import is not held
    to them."""

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        self._restore_fixture("198.51.100.47")
        self.managed = self.env["cloud.host"].create({
            "name": "managed-public", "ip_address": "93.184.216.40",
            "user": "root", "wildcard_domain": "managed.example",
        })

    def _agent(self, url):
        """Ask for a restore of *url* as an agent would."""
        return self.instance.with_user(self.developer).restore_from_url(
            url, from_agent=True,
        )

    def test_an_agent_s_link_on_another_port_or_scheme_is_refused(self):
        with _resolving_to():
            for url, reason in (
                ("https://files.example.com:8443/b.zip", "port 443"),
                ("sftp://files.example.com/b.zip", "https"),
                ("ftp://files.example.com/b.zip", "https"),
                ("https://bob:pw@files.example.com/b.zip", "user or a password"),
            ):
                with self.subTest(url=url), self.assertRaisesRegex(UserError, reason):
                    self._agent(url)

    def test_an_agent_s_link_to_a_server_of_the_panel_is_refused(self):
        """A managed host's public address, the panel's own (an address or
        a network), whatever the name says."""
        CloudHost = type(self.env["cloud.host"])
        for address, panel in (
            ("93.184.216.40", ""),
            ("93.184.216.41", "93.184.216.41"),
            ("93.184.216.99", "93.184.216.0/24"),
        ):
            with (
                self.subTest(address=address),
                _resolving_to(address),
                patch.object(
                    CloudHost, "_hardening_panel_ip", autospec=True,
                    return_value=panel,
                ),
                self.assertRaisesRegex(UserError, "server of this panel"),
            ):
                self._agent("https://innocent.example.com/b.zip")

    def test_an_agent_s_public_link_is_queued_under_its_rules(self):
        with _resolving_to():
            job = self.env["cloud.job"].browse(self._agent(SIGNED))
        self.assertEqual(job.payload["link_policy"], "agent")

    def test_the_rules_hold_again_where_the_link_is_fetched(self):
        """A name that resolved to a public address when it was asked for
        resolves to a server of the panel's when the job runs."""
        with _resolving_to():
            job = self.env["cloud.job"].browse(
                self._agent("https://rebind.example.com/b.zip"),
            )
        with _resolving_to("93.184.216.40"), self.assertRaisesRegex(
            ValueError, "server of this panel",
        ):
            RestoreInstanceExecutor(job, self.host)._checked_link()
        job.sudo().secret_payload = json.dumps(
            {"url": "https://rebind.example.com:8443/b.zip"},
        )
        with _resolving_to(), self.assertRaisesRegex(ValueError, "port 443"):
            RestoreInstanceExecutor(job, self.host)._checked_link()

    def test_the_panel_s_import_still_takes_sftp_and_its_own_servers(self):
        """The dialog's link is the person's, typed in the panel."""
        with _resolving_to("93.184.216.40"):
            job = self.env["cloud.job"].browse(
                self.instance.with_user(self.developer).restore_from_url(
                    "sftp://bob:pw@93.184.216.40:2222/b.zip",
                ),
            )
        self.assertNotIn("link_policy", job.payload)
        self.assertEqual(job._restore_url_link(), "sftp://93.184.216.40:2222/b.zip")

    def test_the_panel_s_own_networks(self):
        """Every managed host's address and the panel's; nothing unparsable,
        nor an unspecified address."""
        import ipaddress
        self.env["cloud.host"].create({
            "name": "unspecified", "ip_address": "0.0.0.0",
            "user": "root", "wildcard_domain": "unspecified.example",
        })
        CloudHost = type(self.env["cloud.host"])
        with patch.object(
            CloudHost, "_hardening_panel_ip", autospec=True, return_value="not an ip",
        ):
            networks = self.env["cloud.host"]._panel_own_networks()
        self.assertIn(ipaddress.ip_network("93.184.216.40/32"), networks)
        self.assertIn(ipaddress.ip_network("198.51.100.47/32"), networks)
        self.assertNotIn(ipaddress.ip_network("0.0.0.0/32"), networks)
