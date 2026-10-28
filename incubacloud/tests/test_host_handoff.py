"""Tests for the host-handoff data path (``backup_download`` with
``handoff='host'`` → ``restore_instance`` mode ``'from_host'``).

The archive a chained restore consumes never round-trips through the
core's RAM or an ``ir.attachment``: on the same host it is restored (and
removed) in place, cross-host it streams source host → core disk →
target host. What is worth pinning here:

  * the path contract — job-keyed, identical on both sides, so the
    consumer recomputes it from ``source_job_id`` alone, and named with
    the panel's secret, so no account on the host can tell it in
    advance (28-oct-2026: it was ``/tmp/.incubacloud-handoff-<id>.zip``,
    0644, a production database any account could read);
  * the producer builds the archive in its own job directory and hands
    it over at the end, into a directory of its own, 0700;
  * the transfer wiring per topology (no transfer same-host, two
    streamed SFTP legs cross-host);
  * the cleanup rules — the source copy is only removed after success,
    which keeps a failed restore retryable; a cancelled chain and a day
    old handoff take it with them;
  * that the attachment relay (``from_job``) still works, now reading
    ``att.raw`` instead of decoding base64.
"""

import asyncio
import hashlib
import hmac
import os
import shlex
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import abstract_executor
from odoo.addons.incubacloud.models.abstract_executor import (
    HANDOFF_SWEEP,
    handoff_archive_path,
    handoff_dir,
)
from odoo.addons.incubacloud.models.registry import executor_registry
from odoo.addons.incubacloud.models.restore_instance_executor import (
    RestoreInstanceExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport


def _make_download_executor(payload, env=None):
    """Return a BackupDownloadExecutor over lightweight doubles.

    :param env: the environment a handoff's name is read with
    """
    from odoo.addons.incubacloud.models.backup_download_executor import (
        BackupDownloadExecutor,
    )

    inst = SimpleNamespace(
        id=7,
        name="demo-inst",
        odoo_version="19.0",
        environment="production",
        postgres_dbname="prod",
    )
    job = SimpleNamespace(
        id=1234, instance_id=inst, payload=payload,
        host_id=SimpleNamespace(id=0),
    )

    ex = object.__new__(BackupDownloadExecutor)
    ex.job = job
    ex.env = env
    ex._inst_dir = lambda i: "~/projects/demo-inst"
    ex._scripts_requested = False
    ex._script_overlay_cache = None
    return ex


def _ran(job):
    """Give *job* a queue job that started, as a download that ran has.

    :param job: a ``cloud.job``
    """
    uuid = f"handoff-ran-{job.id}"
    job.env["queue.job"].sudo().create({
        "uuid": uuid, "name": uuid, "state": "done",
        "method_name": "execute", "model_name": "cloud.job",
        "func_string": "execute()",
        "date_started": fields.Datetime.now(),
    })
    job.queue_job_uuid = uuid


def _discards(env, source):
    """Return the jobs queued to remove *source*'s handoff.

    :param source: the ``backup_download`` job that handed it over
    """
    return env["cloud.job"].sudo().search([
        ("job_type_id.code", "=", "discard_handoff"),
    ]).filtered(
        lambda job: (job.payload or {}).get("source_job_id") == source.id
    )


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
        """Return a host link whose commands all succeed."""
        transport = create_autospec(SSHTransport, instance=True)
        transport.run.return_value = CommandResult("", 0)
        return transport


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
        # Two steps of one chain, as every from_host restore is asked for.
        (source | restore).write({"queue_chain_key": f"chain-{source.id}"})
        return source, self._make_restore_executor(restore)


# ── Producer: backup_download with handoff='host' ────────────────────────────


class TestBackupDownloadHandoff(_RestoreExecutorMixin, TransactionCase):

    def _download(self, payload):
        """Return the producer for *payload*, with this test's environment."""
        return _make_download_executor(payload, env=self.env)

    def test_the_handoff_is_named_with_the_panel_s_secret(self):
        """Recomputable from the job id by the panel, not by the host."""
        secret = self.env["ir.config_parameter"].sudo().get_param(
            "database.secret",
        )
        expected = hmac.new(
            secret.encode(), b"handoff:1234", hashlib.sha256,
        ).hexdigest()[:16]
        self.assertEqual(
            handoff_dir(self.env, 1234), f"/tmp/.incubacloud-handoff-{expected}",
        )
        self.assertEqual(
            handoff_archive_path(self.env, 1234),
            f"{handoff_dir(self.env, 1234)}/archive.zip",
        )
        self.assertNotEqual(
            handoff_dir(self.env, 1234), handoff_dir(self.env, 1235),
        )
        self.assertNotIn("1234", handoff_dir(self.env, 1234))

    def test_without_handoff_it_lands_in_the_job_drop_directory(self):
        ex = self._download({"time": "live", "download_type": "all"})
        self.assertEqual(ex._tmp_archive(), f"{ex._drop_dir()}/backup.zip")

    def _handover(self, cmds, ex):
        """Assert the last step hands the built archive over."""
        label, command, opts = cmds[-1]
        self.assertEqual(label, "Hand the archive over")
        self.assertEqual(opts, {"stop_on_failure": True})
        argv = shlex.split(command)
        self.assertTrue(argv[1].endswith("/backup_download.sh"), argv[1])
        self.assertEqual(argv[2:], [
            "handover", "~/projects/demo-inst", ex._tmp_archive(),
            handoff_dir(self.env, 1234),
        ])

    def test_a_live_handoff_is_dumped_in_the_job_then_handed_over(self):
        """The dump's container sees the job's drop directory alone."""
        ex = self._download({
            "time": "live", "download_type": "all", "handoff": "host",
        })
        cmds = ex.get_commands()
        self.assertEqual(ex._tmp_archive(), f"{ex._drop_dir()}/backup.zip")
        self.assertIn(ex._tmp_archive(), shlex.split(cmds[0][1]))
        self._handover(cmds, ex)

    def test_a_stored_handoff_is_packaged_in_the_job_then_handed_over(self):
        ex = self._download({
            "time": "latest", "download_type": "all", "handoff": "host",
        })
        cmds = ex.get_commands()
        self.assertEqual(cmds[0][0], "Restore full from backup")
        self.assertEqual(ex._tmp_archive(), ex._job_path("backup.zip"))
        self.assertIn(ex._tmp_archive(), shlex.split(cmds[1][1]))
        self._handover(cmds, ex)

    def test_no_handoff_no_handover(self):
        cmds = self._download(
            {"time": "live", "download_type": "all"},
        ).get_commands()
        self.assertNotIn("Hand the archive over", [c[0] for c in cmds])

    def test_a_handoff_sweeps_this_account_s_old_ones_first(self):
        """A day old: a chain whose restore never came for it."""
        ex = self._download({
            "time": "live", "download_type": "all", "handoff": "host",
        })
        transport = self._transport()
        asyncio.run(ex.before_execute(transport))
        runs = [c.args[0] for c in transport.run.await_args_list]
        self.assertEqual(runs[0], HANDOFF_SWEEP)
        self.assertIn(f"mkdir -m 777 {ex._drop_dir()}", runs)
        self.assertIn('-user "$(id -u)"', HANDOFF_SWEEP)
        self.assertIn("-mmin +1440", HANDOFF_SWEEP)

    def test_bad_handoff_value_is_rejected(self):
        ex = self._download({
            "time": "live", "download_type": "all", "handoff": "attachment",
        })
        transport = self._transport()
        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(transport))
        transport.run.assert_not_awaited()

    def test_handoff_host_is_accepted(self):
        ex = self._download({
            "time": "live", "download_type": "all", "handoff": "host",
        })
        asyncio.run(ex.before_execute(self._transport()))  # must not raise


# ── Consumer: restore_instance mode 'from_host' ──────────────────────────────


class TestFromHostPaths(_HandoffBase):

    def test_same_host_restores_the_handoff_archive_in_place(self):
        source, ex = self._restore_pair(self.host_a)
        self.assertEqual(
            ex._remote_path(), handoff_archive_path(self.env, source.id),
        )

    def test_cross_host_stages_in_the_job_s_own_directory(self):
        _, ex = self._restore_pair(self.host_b)
        self.assertEqual(ex._remote_path(), ex._job_path("restore.zip"))

    def test_the_cleanup_step_removes_the_same_path_it_restored(self):
        # The trailing "Remove remote backup file" step is what deletes a
        # same-host handoff archive — it must point at the handoff path.
        source, ex = self._restore_pair(self.host_a)
        ex._inst_dir = lambda i: "~/projects/ho-staging"
        cmds = ex.get_commands()
        rm = next(c for c in cmds if c[0] == "Remove remote backup file")
        # The handoff's whole directory: the restore made its archive the
        # odoo user's, and only the directory's owner removes it.
        self.assertEqual(rm[1], f"rm -rf {handoff_dir(self.env, source.id)}")

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
        self.assertEqual(remote_arg, handoff_archive_path(self.env, source.id))
        # …then pushing that temp file to the target's staging path.
        (up_local, up_remote), _kw = own.upload_file.call_args
        self.assertEqual(up_local, local_arg)
        self.assertEqual(up_remote, ex._job_path("restore.zip"))

    def test_cross_host_success_removes_the_source_copy(self):
        source, ex = self._restore_pair(self.host_b)
        src = self._transport()
        calls = []
        with self._patch_source_transport(src, calls):
            asyncio.run(ex.on_success({}))
        self.assertEqual(calls, [source.host_id])
        (cmd,), _kw = src.run.call_args
        self.assertEqual(cmd, f"rm -rf {handoff_dir(self.env, source.id)}")

    def test_a_restore_from_a_handoff_sweeps_this_host_first(self):
        _, ex = self._restore_pair(self.host_b)
        own = self._transport()
        with self._patch_source_transport(self._transport()):
            asyncio.run(ex.before_execute(own))
        self.assertEqual(own.run.await_args_list[0].args[0], HANDOFF_SWEEP)

    def test_a_restore_cancelled_in_the_queue_queues_the_handoff_s_removal(self):
        """The download ran; its restore never will. Wherever the restore
        was to run, the handoff is on the download's host: a hidden job
        there removes it. The cancellation itself reaches no host."""
        for restore_host in (self.host_a, self.host_b):
            source, ex = self._restore_pair(restore_host)
            _ran(source)
            calls = []
            with self._patch_source_transport(self._transport(), calls):
                ex.on_cancelled_queued()
            with self.subTest(same_host=restore_host == self.host_a):
                self.assertFalse(calls)
                (discard,) = _discards(self.env, source)
                self.assertEqual(discard.host_id, source.host_id)
                self.assertFalse(discard.instance_id)

    def test_a_cancelled_restore_of_another_kind_queues_nothing(self):
        source, ex = self._restore_pair(self.host_a)
        _ran(source)
        ex.job.payload = {"mode": "browser", "local_path": "/nowhere.zip"}
        calls = []
        with self._patch_source_transport(self._transport(), calls):
            ex.on_cancelled_queued()
        self.assertFalse(calls)
        self.assertFalse(_discards(self.env, source))

    def test_nothing_is_queued_for_a_download_that_never_ran(self):
        """A chain cancelled whole in the queue handed nothing over."""
        source, ex = self._restore_pair(self.host_b)
        ex.on_never_run()
        ex.on_cancelled_queued()
        self.assertFalse(_discards(self.env, source))

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
        self.assertEqual(up_remote, ex._job_path("restore.zip"))

    def test_from_job_without_attachment_is_refused(self):
        source = self._create_job("backup_download", self.host_a, self.prod)
        restore = self._create_job(
            "restore_instance", self.host_a, self.staging,
        )
        restore.payload = {"mode": "from_job", "source_job_id": source.id}
        ex = self._make_restore_executor(restore)
        with self.assertRaises(ValueError):
            asyncio.run(ex.before_execute(self._transport()))


class TestARestoreRetriedAfterAFailure(_HandoffBase):
    """A restore that fails keeps the handoff; the next attempt uses it
    and removes it."""

    def _run(self, ex, fail_restore):
        """Run *ex*'s whole job on host A; ``Restore database`` fails if
        *fail_restore*.

        :return: the commands the steps ran, and the exception, if any
        """
        transport = self._transport()

        def _answer(command, *_args, **_kwargs):
            """Exit 1 for the database restore when asked to fail."""
            failed = fail_restore and " restore-db " in command
            return CommandResult("", 1 if failed else 0)

        transport.execute.side_effect = _answer
        exc = None
        with patch.object(
            type(self.host_a), "get_transport", autospec=True,
        ) as get_transport, patch.object(
            type(ex), "on_failure", autospec=True,
        ), patch.object(type(ex), "on_success", autospec=True):
            get_transport.return_value.__aenter__.return_value = transport
            try:
                asyncio.run(ex._async_entry())
            except RuntimeError as error:
                exc = error
        return [c.args[0] for c in transport.execute.await_args_list], exc

    def test_the_archive_survives_the_failure_and_goes_after_success(self):
        self.registry_enter_test_mode()
        source, pair = self._restore_pair(self.host_a)
        first = RestoreInstanceExecutor(pair.job, self.host_a)
        handoff = handoff_dir(self.env, source.id)
        archive = handoff_archive_path(self.env, source.id)
        ran, exc = self._run(first, fail_restore=True)
        self.assertIsInstance(exc, RuntimeError)
        self.assertTrue(any(f" {archive}" in c for c in ran))
        self.assertNotIn(f"rm -rf {handoff}", ran)
        again = RestoreInstanceExecutor(first.job, self.host_a)
        ran, exc = self._run(again, fail_restore=False)
        self.assertIsNone(exc)
        self.assertTrue(any(f" {archive}" in c for c in ran))
        self.assertEqual(ran[-1], f"rm -rf {handoff}")


class TestTheHandoffGoesWithItsChain(_HandoffBase):
    """A restore that will not run takes its handoff with it, without
    reaching a host from the transaction that says so.

    The cancellation runs in a request, in the dispatcher with the
    queues' locks held, or in the transaction recording the end of a
    chain's step; a host that is slow or down would hold them all
    (28-oct-2026: it opened an SSH session from there). A hidden job of
    the download's host removes the archive instead.
    """

    def setUp(self):
        """A download that ran on host A, and its restore on host B, the
        two steps of one chain."""
        super().setUp()
        self.source, ex = self._restore_pair(self.host_b)
        self.restore = ex.job
        (self.source | self.restore).write({"queue_chain_key": "handoff-chain"})
        _ran(self.source)

    def _no_host(self):
        """Patch the hosts' transport to fail the test if anything opens it."""
        return patch.object(
            type(self.env["cloud.host"]), "get_transport", autospec=True,
            side_effect=AssertionError("a host was reached"),
        )

    def _graph(self, job, state, graph="handoff-graph"):
        """Put *job* in a queue_job graph, in *state*."""
        uuid = f"{graph}-{job.id}"
        self.env["queue.job"].sudo().create({
            "uuid": uuid, "name": uuid, "state": state,
            "method_name": "execute", "model_name": "cloud.job",
            "func_string": "execute()", "graph_uuid": graph,
        })
        job.write({"queue_job_uuid": uuid, "state": state})
        return job.queue_job_id

    def test_cancelling_it_in_the_queue_makes_no_ssh_call(self):
        self.restore.write({"state": "queued"})
        with self._no_host() as get_transport:
            cancelled = self.env["cloud.job"]._cancel_queued(
                self.restore, "Cancelled for the test.",
            )
        self.assertEqual(cancelled, self.restore)
        get_transport.assert_not_called()
        (discard,) = _discards(self.env, self.source)
        self.assertEqual(discard.host_id, self.host_a)
        self.assertNotIn("handoff", str(discard.payload))

    def test_a_chain_that_ends_before_the_restore_takes_the_handoff(self):
        """A step between the download and the restore fails: the restore
        is cancelled with the rest of the chain, and the handoff goes."""
        step = self._create_job("restart_instance", self.host_b, self.staging)
        restore = self._create_job(
            "restore_instance", self.host_b, self.staging,
        )
        restore.payload = {"mode": "from_host", "source_job_id": self.source.id}
        (step | restore).write({"queue_chain_key": "handoff-chain"})
        failing = self._graph(step, "started")
        self._graph(restore, "wait_dependencies")
        with self._no_host() as get_transport:
            failing.write({"state": "failed"})
        get_transport.assert_not_called()
        restore.invalidate_recordset(["state"])
        self.assertEqual(restore.state, "cancelled")
        (discard,) = _discards(self.env, self.source)
        self.assertEqual(discard.host_id, self.host_a)

    def test_a_restore_cancelled_before_it_started_takes_the_handoff(self):
        """Dispatched, not started, cancelled through its queue job."""
        queued = self._graph(self.restore, "pending")
        with self._no_host() as get_transport:
            queued.write({"state": "cancelled"})
        get_transport.assert_not_called()
        (discard,) = _discards(self.env, self.source)
        self.assertEqual(discard.host_id, self.host_a)

    def test_a_restore_of_another_chain_leaves_it_alone(self):
        """A restore asked for by hand (``cloud.job.enqueue``) naming a
        download it did not chain with: cancelling it removes nothing.
        The hook runs as the superuser, so reading the download is no
        proof the restore may name it (28-oct-2026: anyone who may queue
        a restore removed the archive another chain was restoring)."""
        for key in (False, "another-chain"):
            with self.subTest(chain=key):
                foreign = self._create_job(
                    "restore_instance", self.host_b, self.staging,
                )
                foreign.write({
                    "payload": {
                        "mode": "from_host", "source_job_id": self.source.id,
                    },
                    "queue_chain_key": key,
                    "state": "queued",
                })
                with self._no_host():
                    self.env["cloud.job"]._cancel_queued(
                        foreign, "Cancelled for the test.",
                    )
                self.assertFalse(_discards(self.env, self.source))

    def test_the_retry_of_its_restore_still_takes_it(self):
        """A retry is not in the chain, but the job it retries is."""
        retry = self._create_job("restore_instance", self.host_b, self.staging)
        retry.write({
            "payload": dict(self.restore.payload),
            "retry_of_id": self.restore.id,
            "state": "queued",
        })
        with self._no_host():
            self.env["cloud.job"]._cancel_queued(retry, "Cancelled for the test.")
        (discard,) = _discards(self.env, self.source)
        self.assertEqual(discard.host_id, self.host_a)

    def test_a_restore_that_failed_keeps_it_for_its_retry(self):
        """Its retry restores the same archive; the sweep takes it if no
        retry comes."""
        queued = self._graph(self.restore, "started")
        queued.write({"state": "failed"})
        self.assertFalse(_discards(self.env, self.source))

    def test_the_hidden_job_removes_the_handoff_on_the_download_s_host(self):
        """It names the directory only on the host, recomputed from the
        download, and waits for nothing there."""
        self.env["cloud.job"].sudo().enqueue(
            self.host_a.id, False, "discard_handoff",
            payload={"source_job_id": self.source.id},
        )
        (discard,) = _discards(self.env, self.source)
        self.assertIn(
            "discard_handoff", self.env["cloud.job"]._get_hidden_job_types(),
        )
        self.assertEqual(
            self.env["cloud.job"]._get_job_type_min_group()["discard_handoff"],
            "group_cloud_manager",
        )
        executor = executor_registry.get("discard_handoff")(
            discard, self.host_a,
        )
        self.assertEqual(
            [step[:2] for step in executor.get_commands()],
            [("Remove the handed-over archive",
              f"rm -rf {handoff_dir(self.env, self.source.id)}")],
        )


class TestAHandoffNobodyWaitsFor(_HandoffBase):
    """A handoff goes when no restore will come for it, and stays while
    one may (28-oct-2026).

    A restore cancelled while its download still ran had its removal
    queued before there was anything to remove: the download handed the
    archive over afterwards, and it stayed until the day-old sweep. And
    that sweep took a handoff whose restore was still waiting its turn.
    """

    def setUp(self):
        """A download on host A that ran, and its restore on host B, the
        two steps of one chain."""
        super().setUp()
        # The download reads its chain on a cursor of its own.
        self.registry_enter_test_mode()
        self.source, ex = self._restore_pair(self.host_b)
        self.restore = ex.job
        (self.source | self.restore).write({
            "queue_chain_key": "awaited-chain",
        })
        self.source.payload = {
            "time": "live", "download_type": "all", "handoff": "host",
        }
        _ran(self.source)

    def _hand_over(self):
        """Run the download's last hook; return what it ran on the host."""
        ex = executor_registry.get("backup_download")(self.source, self.host_a)
        transport = self._transport()
        asyncio.run(ex.after_commands(transport, {}))
        return [c.args[0] for c in transport.run.await_args_list]

    def test_a_handoff_whose_restore_was_cancelled_meanwhile_goes(self):
        self.restore.write({"state": "cancelled"})
        self.assertIn(
            f"rm -rf {handoff_dir(self.env, self.source.id)}", self._hand_over(),
        )

    def test_a_connection_lost_removing_it_does_not_fail_the_download(self):
        """The archive was handed over; if the removal cannot reach the
        host the day's sweep takes it, and the job stays done (N5)."""
        self.restore.write({"state": "cancelled"})
        ex = executor_registry.get("backup_download")(self.source, self.host_a)
        transport = self._transport()
        transport.run.side_effect = ConnectionResetError("lost")
        asyncio.run(ex.after_commands(transport, {}))  # must not raise
        transport.run.assert_awaited_once()
        logged = " ".join(msg for msg, _src in ex._log_buffer)
        self.assertNotIn("was removed from the host", logged)
        self.assertIn("sweep", logged)

    def test_a_handoff_its_restore_waits_for_stays(self):
        self.restore.write({"state": "wait_dependencies"})
        self.assertEqual(self._hand_over(), [])

    def test_a_handoff_nobody_is_known_to_wait_for_stays(self):
        """Without a chain there is no telling: the sweep decides."""
        self.restore.write({"state": "cancelled", "queue_chain_key": False})
        self.source.write({"queue_chain_key": False})
        self.assertEqual(self._hand_over(), [])

    def test_the_sweep_spares_what_a_restore_still_waits_for(self):
        """On the download's host only, and only while the restore may
        still run."""
        name = handoff_dir(self.env, self.source.id).rsplit("/", 1)[1]
        self.restore.write({"state": "wait_dependencies"})
        sweep = abstract_executor.handoff_sweep(self.env, self.host_a)
        self.assertIn(f"! -name {name}", sweep)
        self.assertNotIn(name, abstract_executor.handoff_sweep(
            self.env, self.host_b,
        ))
        self.restore.write({"state": "cancelled"})
        self.assertEqual(
            abstract_executor.handoff_sweep(self.env, self.host_a),
            HANDOFF_SWEEP,
        )

    def test_lab_the_sweep_keeps_the_awaited_one_only(self):
        """Real directories, a day and more old."""
        self.restore.write({"state": "queued"})
        name = handoff_dir(self.env, self.source.id).rsplit("/", 1)[1]
        old = time.time() - 2 * 86400
        with tempfile.TemporaryDirectory() as tmp:
            for each in (name, ".incubacloud-handoff-0000000000000000"):
                Path(tmp, each).mkdir()
                os.utime(Path(tmp, each), (old, old))
            done = subprocess.run(
                ["sh", "-c", abstract_executor.handoff_sweep(
                    self.env, self.host_a, root=tmp,
                )],
                check=False,
            )
            self.assertEqual(done.returncode, 0)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [name])
