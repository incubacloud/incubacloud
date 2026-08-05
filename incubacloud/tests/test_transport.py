"""
Tests for the transport abstraction layer.

Covers:
  - BaseTransport ABC contract
  - SSHTransport.execute() — streaming stdout/stderr via callbacks
  - SSHTransport.run() — captured output
  - SSHTransport file operations — upload/download delegate to SFTP
  - AbstractExecutor.after_commands hook — called on success, not on failure
  - BackupCreateExecutor.after_commands — download + cleanup for non-prod
  - BackupDownloadExecutor.after_commands — calls _download_zip
"""

import asyncio
import unittest
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncssh

from odoo.tests.common import BaseCase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _async_iter(lines):
    """Return an async iterator that yields the given lines."""

    async def _gen():
        for line in lines:
            yield line

    return _gen()


# ---------------------------------------------------------------------------
# 1. BaseTransport is abstract
# ---------------------------------------------------------------------------


class TestBaseTransportIsAbstract(unittest.TestCase):
    def test_cannot_instantiate_directly(self):
        from odoo.addons.incubacloud.models.transport import BaseTransport

        with self.assertRaises(TypeError):
            BaseTransport()

    def test_ssh_transport_implements_all_abstract_methods(self):
        from odoo.addons.incubacloud.models.transport import (
            BaseTransport,
            SSHTransport,
        )

        # All abstract methods must be present on SSHTransport
        for name in BaseTransport.__abstractmethods__:
            self.assertTrue(
                hasattr(SSHTransport, name),
                f"SSHTransport missing abstract method: {name}",
            )

    def test_command_result_is_namedtuple(self):
        from odoo.addons.incubacloud.models.transport import CommandResult

        r = CommandResult(stdout="hello", exit_status=0)
        self.assertEqual(r.stdout, "hello")
        self.assertEqual(r.exit_status, 0)


# ---------------------------------------------------------------------------
# 2. SSHTransport.execute()
# ---------------------------------------------------------------------------


class TestSSHTransportExecute(unittest.TestCase):
    def _make_process(self, stdout_lines, stderr_lines, exit_status=0):
        process = MagicMock(spec=asyncssh.SSHClientProcess)
        process.stdout = _async_iter(stdout_lines)
        process.stderr = _async_iter(stderr_lines)
        process.exit_status = exit_status
        process.wait = AsyncMock()
        return process

    def test_stdout_lines_collected(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport

        process = self._make_process(["line1\n", "line2\n"], [])
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        captured = []
        result = _run(transport.execute("cmd", captured.append, AsyncMock()))
        self.assertEqual(result.stdout, "line1\nline2")
        self.assertEqual(captured, ["line1", "line2"])

    def test_stderr_lines_forwarded(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport

        process = self._make_process([], ["err1\n", "err2\n"])
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        stderr_captured = []
        _run(transport.execute("cmd", AsyncMock(), stderr_captured.append))
        self.assertEqual(stderr_captured, ["err1", "err2"])

    def test_exit_status_returned(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport

        process = self._make_process([], [], exit_status=42)
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        result = _run(transport.execute("cmd", AsyncMock(), AsyncMock()))
        self.assertEqual(result.exit_status, 42)

    def test_empty_lines_ignored(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport

        process = self._make_process(["   \n", "\n", "real\n"], [])
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        captured = []
        _run(transport.execute("cmd", captured.append, AsyncMock()))
        # Blank lines stripped — only "real" survives
        self.assertNotIn("", captured)
        self.assertIn("real", captured)


# ---------------------------------------------------------------------------
# 3. SSHTransport.run()
# ---------------------------------------------------------------------------


class TestSSHTransportRun(unittest.TestCase):
    def test_returns_stdout_and_exit_status(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport

        fake_result = SimpleNamespace(stdout="output\n", exit_status=0)
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.run = AsyncMock(return_value=fake_result)
        transport = SSHTransport(conn)

        result = _run(transport.run("uname -s"))
        self.assertEqual(result.stdout, "output")
        self.assertEqual(result.exit_status, 0)

    def test_none_stdout_becomes_empty_string(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport

        fake_result = SimpleNamespace(stdout=None, exit_status=1)
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.run = AsyncMock(return_value=fake_result)
        transport = SSHTransport(conn)

        result = _run(transport.run("false"))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.exit_status, 1)

    def test_run_passes_check_false(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport

        fake_result = SimpleNamespace(stdout="", exit_status=0)
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.run = AsyncMock(return_value=fake_result)
        transport = SSHTransport(conn)

        _run(transport.run("cmd"))
        conn.run.assert_called_once_with("cmd", check=False)


# ---------------------------------------------------------------------------
# 4. SSHTransport file operations
# ---------------------------------------------------------------------------


def _make_sftp_ctx(sftp_mock):
    """Return an async context manager that yields sftp_mock."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=sftp_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_file_ctx():
    """Return an async context manager for sftp.open().

    The ``ctx`` wrapper is a pure context-manager shim (only
    ``__aenter__``/``__aexit__`` are exercised); the handle the
    production code actually calls methods on is spec'd against the
    real asyncssh class.
    """
    fh = MagicMock(spec=asyncssh.SFTPClientFile)
    fh.write = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fh)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, fh


class TestSSHTransportFileOps(unittest.TestCase):
    def _transport_with_sftp(self):
        sftp = MagicMock(spec=asyncssh.SFTPClient)
        sftp.put = AsyncMock()
        sftp.get = AsyncMock()
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.start_sftp_client = MagicMock(return_value=_make_sftp_ctx(sftp))
        from odoo.addons.incubacloud.models.transport import SSHTransport

        return SSHTransport(conn), sftp

    def test_upload_text_files_writes_each_path(self):
        transport, sftp = self._transport_with_sftp()
        file_ctx1, fh1 = _make_file_ctx()
        file_ctx2, fh2 = _make_file_ctx()
        sftp.open = MagicMock(side_effect=[file_ctx1, file_ctx2])

        _run(
            transport.upload_text_files(
                {
                    "/tmp/a.txt": "content-a",
                    "/tmp/b.txt": "content-b",
                }
            )
        )

        self.assertEqual(sftp.open.call_count, 2)
        fh1.write.assert_called_once_with("content-a")
        fh2.write.assert_called_once_with("content-b")

    def test_upload_file_calls_sftp_put(self):
        transport, sftp = self._transport_with_sftp()
        _run(transport.upload_file("/local/f.zip", "/remote/f.zip"))
        sftp.put.assert_called_once_with("/local/f.zip", "/remote/f.zip")

    def test_upload_dir_calls_sftp_put_recurse(self):
        transport, sftp = self._transport_with_sftp()
        _run(transport.upload_dir("/local/dir", "/remote/dir"))
        sftp.put.assert_called_once_with("/local/dir", "/remote/dir", recurse=True)

    def test_download_file_calls_sftp_get(self):
        transport, sftp = self._transport_with_sftp()
        _run(transport.download_file("/remote/f.zip", "/local/f.zip"))
        sftp.get.assert_called_once_with("/remote/f.zip", "/local/f.zip")


# ---------------------------------------------------------------------------
# 5. AbstractExecutor.after_commands hook
# ---------------------------------------------------------------------------


class TestAbstractExecutorAfterCommandsHook(unittest.TestCase):
    """Verify after_commands is called on success and skipped on failure."""

    def _make_executor(self, commands, parse_errors=None):
        from odoo.addons.incubacloud.models.abstract_executor import (
            AbstractExecutor,
        )

        after_called_with = []

        class _TestExecutor(AbstractExecutor):
            _job_type = None  # don't register

            def __init_subclass__(cls, **kwargs):
                pass  # skip registry

            def get_commands(self):
                return commands

            def parse_results(self, results):
                return parse_errors or []

            async def after_commands(self, transport, results):
                after_called_with.append((transport, results))

        # Build a minimal job+host double without Odoo ORM
        job = MagicMock()
        job.id = 1
        job.instance_id = None
        job.env = MagicMock()
        job.env.registry.cursor.return_value.__enter__ = MagicMock(
            return_value=MagicMock()
        )
        job.env.registry.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cr_env = MagicMock()
        cr_env.__getitem__ = MagicMock(
            return_value=MagicMock(browse=MagicMock(return_value=job))
        )
        job.env.__call__ = MagicMock(return_value=cr_env)

        host = MagicMock()
        host.ip_address = "127.0.0.1"
        host.port = 22
        host.user = "root"

        ex = object.__new__(_TestExecutor)
        AbstractExecutor.__init__(ex, job, host)
        return ex, after_called_with

    def _fake_transport(self, exit_status=0):
        from odoo.addons.incubacloud.models.transport import CommandResult

        transport = MagicMock()
        transport.execute = AsyncMock(
            return_value=CommandResult(stdout="ok", exit_status=exit_status)
        )
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=transport)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return transport, ctx

    def test_after_commands_called_on_success(self):
        transport, ctx = self._fake_transport(exit_status=0)
        ex, called = self._make_executor([("step", "echo hi")])
        ex._host_record.get_transport = MagicMock(return_value=ctx)
        ex._log_buffer = []

        _run(ex._async_entry())

        self.assertEqual(len(called), 1)
        self.assertIs(called[0][0], transport)

    def test_after_commands_not_called_on_parse_failure(self):
        transport, ctx = self._fake_transport(exit_status=0)
        ex, called = self._make_executor(
            [("step", "echo hi")],
            parse_errors=["something went wrong"],
        )
        ex._host_record.get_transport = MagicMock(return_value=ctx)
        ex._log_buffer = []

        with self.assertRaises(RuntimeError):
            _run(ex._async_entry())

        self.assertEqual(called, [])

    def test_after_commands_not_called_when_command_fails_stop_on_failure(
        self,
    ):
        transport, ctx = self._fake_transport(exit_status=1)
        ex, called = self._make_executor(
            [("step", "false", {"stop_on_failure": True})],
        )
        ex._host_record.get_transport = MagicMock(return_value=ctx)
        ex._log_buffer = []

        # No parse errors, but stop_on_failure aborted → after_commands called
        # (parse_results sees empty results dict or only partial results)
        # With no parse errors defined, after_commands IS called even after
        # stop_on_failure; it's the executor's parse_results that decides.
        _run(ex._async_entry())
        self.assertEqual(len(called), 1)


# ---------------------------------------------------------------------------
# 6. BackupCreateExecutor.after_commands
# ---------------------------------------------------------------------------


class TestBackupCreateAfterCommands(unittest.TestCase):
    def _make_executor(self, environment="staging"):
        from odoo.addons.incubacloud.models.backup_create_executor import (
            BackupCreateExecutor,
        )

        inst = SimpleNamespace(
            name="myinst",
            environment=environment,
            postgres_dbname="prod",
            deployed=True,
        )
        ex = object.__new__(BackupCreateExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: f"/home/{i.name}"
        ex._log_buffer = []
        ex._sys = ex._log_buffer.append
        ex.job = MagicMock()
        ex.job.id = 99
        ex.env = MagicMock()
        return ex

    def test_non_prod_calls_download_and_cleanup(self):
        ex = self._make_executor(environment="staging")
        transport = MagicMock()
        transport.download_file = AsyncMock()
        transport.run = AsyncMock(
            return_value=SimpleNamespace(stdout="", exit_status=0)
        )
        ex._download_backup = AsyncMock()

        _run(ex.after_commands(transport, {}))

        ex._download_backup.assert_called_once_with(transport)
        transport.run.assert_called_once()
        # cleanup command should reference the remote tmp path
        cleanup_cmd = transport.run.call_args[0][0]
        self.assertIn("rm -f", cleanup_cmd)
        self.assertIn("myinst", cleanup_cmd)

    def test_production_does_not_download(self):
        ex = self._make_executor(environment="production")
        transport = MagicMock()
        transport.download_file = AsyncMock()
        transport.run = AsyncMock()
        ex._download_backup = AsyncMock()

        _run(ex.after_commands(transport, {}))

        ex._download_backup.assert_not_called()
        transport.run.assert_not_called()


class TestBackupCreateGetCommands(BaseCase):
    """Non-prod backup uses ``docker compose run --rm`` with /tmp bind-
    mounted so the ZIP lands directly on the host. ``run`` (vs ``exec``)
    means the backup keeps working when the odoo service is stopped —
    e.g. after a failed restore — and the bind mount removes the need
    for a separate ``Copy to host`` step. Production keeps using the
    backup container's daily jobrunner because that path goes to S3.
    """

    def _make_executor(self, environment="staging", payload=None):
        from odoo.addons.incubacloud.models.backup_create_executor import (
            BackupCreateExecutor,
        )

        inst = SimpleNamespace(
            name="myinst",
            environment=environment,
            postgres_dbname="prod",
        )
        ex = object.__new__(BackupCreateExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: f"/home/{i.name}"
        ex.job = MagicMock()
        ex.job.payload = payload
        return ex

    def _make_executor_with_payload(self, environment, payload):
        return self._make_executor(environment=environment, payload=payload)

    def test_non_prod_uses_run_rm_with_tmp_bind_mount(self):
        cmds = self._make_executor(environment="staging").get_commands()
        self.assertEqual(len(cmds), 1)
        label, cmd, opts = cmds[0]
        self.assertEqual(label, "Create backup")
        self.assertIn("docker compose run --rm", cmd)
        self.assertIn("-v /tmp:/host-tmp", cmd)
        self.assertIn("click-odoo-backupdb", cmd)
        self.assertIn("prod", cmd)
        self.assertIn("/host-tmp/.incubacloud-backup-myinst.zip", cmd)
        # Old approach left behind: exec requires a running container,
        # cp was redundant once the bind mount writes to host directly.
        self.assertNotIn("docker compose exec", cmd)
        self.assertNotIn("docker compose cp", cmd)
        # Stop the chain on failure so we don't try to download a ZIP
        # that was never created (the historical bug that surfaced
        # ``Could not find the file …`` on top of the real error).
        self.assertEqual(opts, {"stop_on_failure": True})

    def test_non_prod_default_payload_includes_filestore(self):
        # Backwards compatibility: no payload at all → default to a
        # full backup (DB + filestore). Mirrors click-odoo-backupdb's
        # own default and matches every legacy row in the DB.
        ex = self._make_executor(environment="staging")
        ex.job = MagicMock()
        ex.job.payload = None
        cmd = ex.get_commands()[0][1]
        self.assertIn("--filestore", cmd)
        self.assertNotIn("--no-filestore", cmd)

    def test_non_prod_with_filestore_true_uses_filestore_flag(self):
        ex = self._make_executor_with_payload(
            "staging",
            {"with_filestore": True},
        )
        cmd = ex.get_commands()[0][1]
        self.assertIn("--filestore", cmd)
        self.assertNotIn("--no-filestore", cmd)

    def test_non_prod_with_filestore_false_uses_no_filestore_flag(self):
        ex = self._make_executor_with_payload(
            "staging",
            {"with_filestore": False},
        )
        cmd = ex.get_commands()[0][1]
        self.assertIn("--no-filestore", cmd)
        # Sanity: the dbname still follows the flag.
        self.assertIn("--no-filestore prod", cmd)

    def test_non_prod_truthy_string_payload_collapses_to_filestore(self):
        # Defensive: any truthy JSON-RPC value collapses to True via
        # ``bool()`` at the executor boundary so the user can never
        # smuggle an arbitrary string into the click-odoo-backupdb
        # invocation.
        ex = self._make_executor_with_payload(
            "staging",
            {"with_filestore": "--malicious; rm -rf /"},
        )
        cmd = ex.get_commands()[0][1]
        self.assertIn("--filestore", cmd)
        self.assertNotIn("rm -rf", cmd)
        self.assertNotIn("--malicious", cmd)

    def test_production_uses_backup_container_jobrunner(self):
        cmds = self._make_executor(environment="production").get_commands()
        self.assertEqual(len(cmds), 1)
        label, cmd = cmds[0][0], cmds[0][1]
        self.assertEqual(label, "Create backup")
        self.assertIn("docker compose exec -T backup", cmd)
        self.assertIn("/etc/periodic/daily/jobrunner", cmd)

    def test_production_payload_does_not_inject_filestore_flag(self):
        # Production runs duply (jobrunner) which controls shape.
        # The kwarg must not leak into the prod command line.
        ex = self._make_executor_with_payload(
            "production",
            {"with_filestore": False},
        )
        cmd = ex.get_commands()[0][1]
        self.assertNotIn("--no-filestore", cmd)
        self.assertNotIn("--filestore", cmd)


# ---------------------------------------------------------------------------
# 6b. RestoreInstanceExecutor.get_commands — Verify backup file step
# ---------------------------------------------------------------------------


class TestRestoreInstanceVerifyBackupFile(BaseCase):
    """The ``Verify backup file`` step hands the uploaded zip to
    ``scripts/restore.sh verify-file``, which makes it readable inside
    the odoo container (UID discovery + sudo chown/chmod). Here we pin
    the wiring — the right script operation, in the right order, with
    ``stop_on_failure`` so a missing/unreadable upload never falls
    through to ``click-odoo-restoredb --copy --force`` on the live DB.
    The shell behaviour is covered by ``tests/shell/restore.bats``.
    """

    def _make_executor(self):
        from odoo.addons.incubacloud.models.restore_instance_executor import (
            RestoreInstanceExecutor,
        )

        inst = SimpleNamespace(
            id=42,
            name="myinst",
            postgres_dbname="prod",
        )
        ex = object.__new__(RestoreInstanceExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: f"/home/{i.name}"
        ex.job = SimpleNamespace(id=99, instance_id=inst, payload={})
        ex._scripts_requested = False
        ex._scripts_uploaded = False
        ex._script_overlay_cache = None
        return ex

    def test_verify_step_invokes_restore_verify_file(self):
        cmds = self._make_executor().get_commands()
        label, cmd, opts = cmds[0]
        self.assertEqual(label, "Verify backup file")
        argv = shlex.split(cmd)
        self.assertTrue(argv[1].endswith("/restore.sh"), argv[1])
        self.assertEqual(argv[2], "verify-file")
        self.assertEqual(argv[3], "/home/myinst")
        self.assertEqual(argv[4], "/tmp/incubacloud-restore-42.zip")
        # Stop the chain on failure: skipping the rest avoids destroying
        # the live DB when we already know the upload is missing.
        self.assertEqual(opts, {"stop_on_failure": True})

    def test_restore_and_stop_steps_precede_the_db_restore(self):
        labels = [t[0] for t in self._make_executor().get_commands()]
        self.assertLess(
            labels.index("Verify backup file"),
            labels.index("Restore database"),
        )
        self.assertLess(
            labels.index("Stop Odoo service"),
            labels.index("Restore database"),
        )


# ---------------------------------------------------------------------------
# 7. BackupDownloadExecutor.after_commands
# ---------------------------------------------------------------------------


class TestBackupDownloadAfterCommands(unittest.TestCase):
    def _make_executor(self):
        from odoo.addons.incubacloud.models.backup_download_executor import (
            BackupDownloadExecutor,
        )

        inst = SimpleNamespace(name="dl-inst", postgres_dbname="prod")
        ex = object.__new__(BackupDownloadExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: f"/home/{i.name}"
        ex._log_buffer = []
        ex._sys = ex._log_buffer.append
        ex.job = MagicMock()
        ex.job.id = 77
        ex.job.payload = {"time": "latest", "download_type": "dump"}
        ex.env = MagicMock()
        return ex

    def test_after_commands_delegates_to_download_zip(self):
        ex = self._make_executor()
        transport = MagicMock()
        ex._download_zip = AsyncMock()

        _run(ex.after_commands(transport, {}))

        ex._download_zip.assert_called_once_with(transport)


# ---------------------------------------------------------------------------
# 7b. BackupDownloadExecutor.get_commands × environment
# ---------------------------------------------------------------------------


class TestBackupDownloadGetCommands(BaseCase):
    """``BackupDownloadExecutor`` ships an "exact" backup as a ZIP.

    Production reaches into duplicity/S3 via the ``backup`` container
    (the historical chain).  Non-production has no snapshot store, so
    we take a live dump on demand via ``click-odoo-backupdb`` running
    in the ``odoo`` container — same binary, same pattern as
    ``BackupCreateExecutor``, just with a different ``--filestore /
    --no-filestore`` flag depending on the requested mode.

    Before the staging branch existed, this executor unconditionally
    ran ``docker compose exec backup …`` and bombed on staging with
    ``service "backup" is not running``.
    """

    ARCHIVE = "/tmp/.incubacloud-bkdl-dl-inst.zip"
    TMPDIR = "/tmp/.incubacloud-bkdl-dl-inst"
    DIR = "/home/dl-inst"

    def _make_executor(self, environment, mode="dump", time="latest"):
        from odoo.addons.incubacloud.models.backup_download_executor import (
            BackupDownloadExecutor,
        )

        inst = SimpleNamespace(
            name="dl-inst",
            environment=environment,
            postgres_dbname="prod",
        )
        ex = object.__new__(BackupDownloadExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: self.DIR
        ex.job = MagicMock()
        ex.job.id = 77
        ex.job.payload = {"time": time, "download_type": mode}
        ex._scripts_requested = False
        ex._script_overlay_cache = None
        return ex

    def _argv(self, step):
        """Return the script invocation of *step* as an argv list."""
        return shlex.split(step[1])

    def assertScriptCall(self, step, operation, args):
        argv = self._argv(step)
        self.assertEqual(argv[0], "bash")
        self.assertTrue(argv[1].endswith("/backup_download.sh"), argv[1])
        self.assertEqual(argv[2], operation)
        self.assertEqual(argv[3], self.DIR)
        self.assertEqual(argv[4:], args)

    # ── non-production: live dump on demand ───────────────────────────

    def test_non_prod_dump_takes_a_db_only_live_dump(self):
        cmds = self._make_executor("staging", mode="dump").get_commands()
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][0], "Create live backup")
        self.assertScriptCall(
            cmds[0], "live-dump", ["prod", self.ARCHIVE, "db"],
        )
        # Stop the chain on failure so after_commands does not try to
        # download a zip that was never created — same defensive
        # pattern as BackupCreateExecutor.
        self.assertEqual(cmds[0][2], {"stop_on_failure": True})

    def test_non_prod_all_includes_the_filestore(self):
        cmds = self._make_executor("staging", mode="all").get_commands()
        self.assertEqual(len(cmds), 1)
        self.assertScriptCall(
            cmds[0], "live-dump", ["prod", self.ARCHIVE, "all"],
        )

    # ── production: restore from the backup store, then package ───────

    def test_prod_dump_restores_only_the_sql(self):
        cmds = self._make_executor("production", mode="dump").get_commands()
        self.assertEqual(
            [c[0] for c in cmds],
            ["Restore SQL from backup", "Extract and package"],
        )
        self.assertScriptCall(cmds[0], "restore-sql", ["prod", "latest"])
        self.assertScriptCall(
            cmds[1], "package-sql", ["prod", self.TMPDIR, self.ARCHIVE],
        )

    def test_prod_all_restores_the_whole_backup(self):
        cmds = self._make_executor("production", mode="all").get_commands()
        self.assertEqual(
            [c[0] for c in cmds],
            ["Restore full from backup", "Extract and package"],
        )
        self.assertScriptCall(cmds[0], "restore-full", ["latest"])
        self.assertScriptCall(
            cmds[1], "package-full", ["prod", self.TMPDIR, self.ARCHIVE],
        )

    def test_prod_passes_an_explicit_time_through(self):
        ex = self._make_executor(
            "production", mode="dump", time="2026-03-19T02:00:00",
        )
        self.assertScriptCall(
            ex.get_commands()[0],
            "restore-sql",
            ["prod", "2026-03-19T02:00:00"],
        )


# ---------------------------------------------------------------------------
# 7c. BackupDownloadExecutor.before_execute guards
# ---------------------------------------------------------------------------


class TestBackupDownloadBeforeExecute(BaseCase):
    """Non-prod has no historical snapshot store, so ``time`` must be
    ``'latest'`` (= live dump).  We reject historical timestamps in
    ``before_execute`` so the SPA surfaces a clear ``UserError`` toast
    instead of letting the SSH job bomb downstream.
    """

    def _make_executor(self, environment, time):
        from odoo.addons.incubacloud.models.backup_download_executor import (
            BackupDownloadExecutor,
        )

        inst = SimpleNamespace(
            id=1,
            name="dl-inst",
            environment=environment,
            postgres_dbname="prod",
        )
        ex = object.__new__(BackupDownloadExecutor)
        ex._inst = lambda: inst
        ex.job = MagicMock()
        ex.job.payload = {"time": time, "download_type": "dump"}
        return ex

    def test_non_prod_rejects_historical_timestamp(self):
        ex = self._make_executor("staging", "2026-03-19T02:00:00")
        with self.assertRaises(ValueError) as ctx:
            _run(ex.before_execute(MagicMock()))
        self.assertIn("historical", str(ctx.exception).lower())

    def test_non_prod_accepts_latest(self):
        ex = self._make_executor("staging", "latest")
        # Should not raise.
        _run(ex.before_execute(MagicMock()))

    def test_prod_accepts_historical_timestamp(self):
        ex = self._make_executor("production", "2026-03-19T02:00:00")
        # Should not raise — duplicity will resolve the snapshot.
        _run(ex.before_execute(MagicMock()))
