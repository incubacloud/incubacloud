"""Tests for the host-handoff data path (``backup_download`` with
``handoff='host'`` → ``restore_instance`` mode ``'from_host'``).

The archive a chained restore consumes never round-trips through the
core's RAM or an ``ir.attachment``: on the same host it is restored (and
removed) in place, cross-host it streams source host → core disk →
target host. What is worth pinning here:

  * the path contract — job-keyed, identical on both sides, so the
    consumer recomputes it from ``source_job_id`` alone;
  * the transfer wiring per topology (no transfer same-host, two
    streamed SFTP legs cross-host);
  * the cleanup rules — the source copy is only removed after success,
    which keeps a failed restore retryable;
  * that the attachment relay (``from_job``) still works, now reading
    ``att.raw`` instead of decoding base64.
"""

import asyncio
import shlex
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models.abstract_executor import (
    handoff_archive_path,
)
from odoo.addons.incubacloud.models.transport import SSHTransport


def _make_download_executor(payload):
    """Return a BackupDownloadExecutor over lightweight doubles."""
    from odoo.addons.incubacloud.models.backup_download_executor import (
        BackupDownloadExecutor,
    )

    inst = SimpleNamespace(
        id=7,
        name="demo-inst",
        environment="production",
        postgres_dbname="prod",
    )
    job = SimpleNamespace(id=1234, instance_id=inst, payload=payload)

    ex = object.__new__(BackupDownloadExecutor)
    ex.job = job
    ex._inst_dir = lambda i: "~/projects/demo-inst"
    ex._scripts_requested = False
    ex._script_overlay_cache = None
    return ex


class _RestoreExecutorMixin:
    """Shared builders for restore-executor tests over real records."""

    def _make_restore_executor(self, job):
        from odoo.addons.incubacloud.models.restore_instance_executor import (
            RestoreInstanceExecutor,
        )

        ex = object.__new__(RestoreInstanceExecutor)
        ex.job = job
        ex.env = job.env
        ex._log_buffer = []
        ex._scripts_requested = False
        ex._scripts_uploaded = False
        ex._script_overlay_cache = None
        return ex

    def _create_job(self, code, host, instance):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        return self.env["cloud.job"].create({
            "host_id": host.id,
            "instance_id": instance.id,
            "job_type_id": job_type.id,
            "name": f"{code} test job",
        })

    def _transport(self):
        return create_autospec(SSHTransport, instance=True)


class _HandoffBase(_RestoreExecutorMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "ho-proj"})
        self.host_a = self.env["cloud.host"].create({
            "name": "ho-host-a", "ip_address": "10.0.3.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "hoa.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.host_b = self.env["cloud.host"].create({
            "name": "ho-host-b", "ip_address": "10.0.3.2", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "hob.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.prod = self.env["cloud.instance"].create({
            "name": "ho-prod", "project_id": self.project.id,
            "environment": "production", "host_id": self.host_a.id,
            "state": "deployed",
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "ho-staging", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host_a.id,
            "state": "deployed",
        })

    def _restore_pair(self, restore_host, payload_extra=None):
        """Create a source download job on host A and a from_host restore
        job for the staging on *restore_host*; return (source, executor).
        """
        source = self._create_job("backup_download", self.host_a, self.prod)
        restore = self._create_job(
            "restore_instance", restore_host, self.staging,
        )
        restore.payload = {
            "mode": "from_host",
            "source_job_id": source.id,
            **(payload_extra or {}),
        }
        return source, self._make_restore_executor(restore)


# ── Producer: backup_download with handoff='host' ────────────────────────────


class TestBackupDownloadHandoff(BaseCase):

    def test_handoff_archive_is_keyed_by_job_id(self):
        ex = _make_download_executor({
            "time": "live", "download_type": "all", "handoff": "host",
        })
        self.assertEqual(
            ex._tmp_archive(), "/tmp/.incubacloud-handoff-1234.zip",
        )
        self.assertEqual(ex._tmp_archive(), handoff_archive_path(ex.job.id))

    def test_without_handoff_the_classic_path_is_kept(self):
        ex = _make_download_executor({"time": "live", "download_type": "all"})
        self.assertEqual(
            ex._tmp_archive(), "/tmp/.incubacloud-bkdl-demo-inst.zip",
        )

    def test_handoff_path_reaches_the_live_dump_script(self):
        ex = _make_download_executor({
            "time": "live", "download_type": "all", "handoff": "host",
        })
        argv = shlex.split(ex.get_commands()[0][1])
        self.assertIn("/tmp/.incubacloud-handoff-1234.zip", argv)

    def test_handoff_path_reaches_the_duplicity_package_script(self):
        cmds = _make_download_executor({
            "time": "latest", "download_type": "all", "handoff": "host",
        }).get_commands()
        self.assertEqual(cmds[0][0], "Restore full from backup")
        argv = shlex.split(cmds[1][1])
        self.assertIn("/tmp/.incubacloud-handoff-1234.zip", argv)

    def test_bad_handoff_value_is_rejected(self):
        ex = _make_download_executor({
            "time": "live", "download_type": "all", "handoff": "attachment",
        })
        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(None))

    def test_handoff_host_is_accepted(self):
        ex = _make_download_executor({
            "time": "live", "download_type": "all", "handoff": "host",
        })
        asyncio.run(ex.before_execute(None))  # must not raise


# ── Consumer: restore_instance mode 'from_host' ──────────────────────────────


class TestFromHostPaths(_HandoffBase):

    def test_same_host_restores_the_handoff_archive_in_place(self):
        source, ex = self._restore_pair(self.host_a)
        self.assertEqual(ex._remote_path(), handoff_archive_path(source.id))

    def test_cross_host_stages_at_the_instance_keyed_path(self):
        _, ex = self._restore_pair(self.host_b)
        self.assertEqual(
            ex._remote_path(),
            f"/tmp/incubacloud-restore-{self.staging.id}.zip",
        )

    def test_the_cleanup_step_removes_the_same_path_it_restored(self):
        # The trailing "Remove remote backup file" step is what deletes a
        # same-host handoff archive — it must point at the handoff path.
        source, ex = self._restore_pair(self.host_a)
        ex._inst_dir = lambda i: "~/projects/ho-staging"
        cmds = ex.get_commands()
        rm = next(c for c in cmds if c[0] == "Remove remote backup file")
        self.assertIn(handoff_archive_path(source.id), rm[1])

    def test_missing_source_job_is_refused(self):
        _, ex = self._restore_pair(self.host_a)
        ex.job.payload = {"mode": "from_host"}
        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(self._transport()))

    def test_unknown_source_job_is_refused(self):
        _, ex = self._restore_pair(self.host_a)
        ex.job.payload = {"mode": "from_host", "source_job_id": 99999999}
        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(self._transport()))


class TestFromHostTransfer(_HandoffBase):

    def _patch_source_transport(self, src_transport, calls=None):
        """Patch ``cloud.host.get_transport`` to yield *src_transport*,
        recording each opening host in *calls*."""

        @asynccontextmanager
        async def fake_get_transport(host_self):
            if calls is not None:
                calls.append(host_self)
            yield src_transport

        return patch.object(
            type(self.env["cloud.host"]), "get_transport", fake_get_transport,
        )

    def test_same_host_transfers_nothing(self):
        _, ex = self._restore_pair(self.host_a)
        own = self._transport()
        calls = []
        with self._patch_source_transport(self._transport(), calls):
            asyncio.run(ex.before_execute(own))
        self.assertFalse(calls)
        own.download_file.assert_not_called()
        own.upload_file.assert_not_called()

    def test_cross_host_streams_source_to_core_to_target(self):
        source, ex = self._restore_pair(self.host_b)
        own = self._transport()
        src = self._transport()
        calls = []
        with self._patch_source_transport(src, calls):
            asyncio.run(ex.before_execute(own))
        # One SFTP session against the source host…
        self.assertEqual(calls, [source.host_id])
        # …pulling the handoff archive into a core temp file…
        (remote_arg, local_arg), _kw = src.download_file.call_args
        self.assertEqual(remote_arg, handoff_archive_path(source.id))
        # …then pushing that temp file to the target's staging path.
        (up_local, up_remote), _kw = own.upload_file.call_args
        self.assertEqual(up_local, local_arg)
        self.assertEqual(
            up_remote, f"/tmp/incubacloud-restore-{self.staging.id}.zip",
        )

    def test_cross_host_success_removes_the_source_copy(self):
        source, ex = self._restore_pair(self.host_b)
        src = self._transport()
        calls = []
        with self._patch_source_transport(src, calls):
            asyncio.run(ex.on_success({}))
        self.assertEqual(calls, [source.host_id])
        (cmd,), _kw = src.run.call_args
        self.assertEqual(cmd, f"rm -f {handoff_archive_path(source.id)}")

    def test_same_host_success_opens_no_extra_session(self):
        # The trailing rm step already removed the archive; a second SSH
        # session to "clean up" would be pure overhead.
        _, ex = self._restore_pair(self.host_a)
        calls = []
        with self._patch_source_transport(self._transport(), calls):
            asyncio.run(ex.on_success({}))
        self.assertFalse(calls)


# ── The attachment relay still works (and reads bytes, not base64) ──────────


class TestFromJobStillWorks(_HandoffBase):

    def test_from_job_uploads_the_attachment_bytes(self):
        source = self._create_job("backup_download", self.host_a, self.prod)
        self.env["ir.attachment"].create({
            "name": "ho-prod-backup-x-full.zip",
            "type": "binary",
            "raw": b"zip-bytes",
            "res_model": "cloud.job",
            "res_id": source.id,
        })
        restore = self._create_job(
            "restore_instance", self.host_a, self.staging,
        )
        restore.payload = {"mode": "from_job", "source_job_id": source.id}
        ex = self._make_restore_executor(restore)
        own = self._transport()
        asyncio.run(ex.before_execute(own))
        (up_local, up_remote), _kw = own.upload_file.call_args
        self.assertEqual(
            up_remote, f"/tmp/incubacloud-restore-{self.staging.id}.zip",
        )

    def test_from_job_without_attachment_is_refused(self):
        source = self._create_job("backup_download", self.host_a, self.prod)
        restore = self._create_job(
            "restore_instance", self.host_a, self.staging,
        )
        restore.payload = {"mode": "from_job", "source_job_id": source.id}
        ex = self._make_restore_executor(restore)
        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(self._transport()))
