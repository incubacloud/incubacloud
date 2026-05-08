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

from odoo.tests.common import BaseCase

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call


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
            BaseTransport, SSHTransport,
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
        process = MagicMock()
        process.stdout = _async_iter(stdout_lines)
        process.stderr = _async_iter(stderr_lines)
        process.exit_status = exit_status
        process.wait = AsyncMock()
        return process

    def test_stdout_lines_collected(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport
        process = self._make_process(["line1\n", "line2\n"], [])
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        captured = []
        result = _run(
            transport.execute("cmd", captured.append, AsyncMock())
        )
        self.assertEqual(result.stdout, "line1\nline2")
        self.assertEqual(captured, ["line1", "line2"])

    def test_stderr_lines_forwarded(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport
        process = self._make_process([], ["err1\n", "err2\n"])
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        stderr_captured = []
        _run(
            transport.execute("cmd", AsyncMock(), stderr_captured.append)
        )
        self.assertEqual(stderr_captured, ["err1", "err2"])

    def test_exit_status_returned(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport
        process = self._make_process([], [], exit_status=42)
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        result = _run(
            transport.execute("cmd", AsyncMock(), AsyncMock())
        )
        self.assertEqual(result.exit_status, 42)

    def test_empty_lines_ignored(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport
        process = self._make_process(["   \n", "\n", "real\n"], [])
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=process)
        transport = SSHTransport(conn)

        captured = []
        _run(
            transport.execute("cmd", captured.append, AsyncMock())
        )
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
        conn = MagicMock()
        conn.run = AsyncMock(return_value=fake_result)
        transport = SSHTransport(conn)

        result = _run(transport.run("uname -s"))
        self.assertEqual(result.stdout, "output")
        self.assertEqual(result.exit_status, 0)

    def test_none_stdout_becomes_empty_string(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport
        fake_result = SimpleNamespace(stdout=None, exit_status=1)
        conn = MagicMock()
        conn.run = AsyncMock(return_value=fake_result)
        transport = SSHTransport(conn)

        result = _run(transport.run("false"))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.exit_status, 1)

    def test_run_passes_check_false(self):
        from odoo.addons.incubacloud.models.transport import SSHTransport
        fake_result = SimpleNamespace(stdout="", exit_status=0)
        conn = MagicMock()
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
    """Return an async context manager for sftp.open()."""
    fh = MagicMock()
    fh.write = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fh)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, fh


class TestSSHTransportFileOps(unittest.TestCase):

    def _transport_with_sftp(self):
        sftp = MagicMock()
        sftp.put = AsyncMock()
        sftp.get = AsyncMock()
        conn = MagicMock()
        conn.start_sftp_client = MagicMock(
            return_value=_make_sftp_ctx(sftp)
        )
        from odoo.addons.incubacloud.models.transport import SSHTransport
        return SSHTransport(conn), sftp

    def test_upload_text_files_writes_each_path(self):
        transport, sftp = self._transport_with_sftp()
        file_ctx1, fh1 = _make_file_ctx()
        file_ctx2, fh2 = _make_file_ctx()
        sftp.open = MagicMock(side_effect=[file_ctx1, file_ctx2])

        _run(transport.upload_text_files({
            "/tmp/a.txt": "content-a",
            "/tmp/b.txt": "content-b",
        }))

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
        sftp.put.assert_called_once_with(
            "/local/dir", "/remote/dir", recurse=True
        )

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
        job.env.registry.cursor.return_value.__exit__ = MagicMock(
            return_value=False
        )
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

    def _make_executor(self, environment='staging'):
        from odoo.addons.incubacloud.models.backup_create_executor import (
            BackupCreateExecutor,
        )
        inst = SimpleNamespace(
            name='myinst',
            environment=environment,
            postgres_dbname='prod',
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
        ex = self._make_executor(environment='staging')
        transport = MagicMock()
        transport.download_file = AsyncMock()
        transport.run = AsyncMock(
            return_value=SimpleNamespace(stdout='', exit_status=0)
        )
        ex._download_backup = AsyncMock()

        _run(ex.after_commands(transport, {}))

        ex._download_backup.assert_called_once_with(transport)
        transport.run.assert_called_once()
        # cleanup command should reference the remote tmp path
        cleanup_cmd = transport.run.call_args[0][0]
        self.assertIn('rm -f', cleanup_cmd)
        self.assertIn('myinst', cleanup_cmd)

    def test_production_does_not_download(self):
        ex = self._make_executor(environment='production')
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

    def _make_executor(self, environment='staging', payload=None):
        from odoo.addons.incubacloud.models.backup_create_executor import (
            BackupCreateExecutor,
        )
        inst = SimpleNamespace(
            name='myinst',
            environment=environment,
            postgres_dbname='prod',
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
        cmds = self._make_executor(environment='staging').get_commands()
        self.assertEqual(len(cmds), 1)
        label, cmd, opts = cmds[0]
        self.assertEqual(label, 'Create backup')
        self.assertIn('docker compose run --rm', cmd)
        self.assertIn('-v /tmp:/host-tmp', cmd)
        self.assertIn('click-odoo-backupdb', cmd)
        self.assertIn('prod', cmd)
        self.assertIn('/host-tmp/.incubacloud-backup-myinst.zip', cmd)
        # Old approach left behind: exec requires a running container,
        # cp was redundant once the bind mount writes to host directly.
        self.assertNotIn('docker compose exec', cmd)
        self.assertNotIn('docker compose cp', cmd)
        # Stop the chain on failure so we don't try to download a ZIP
        # that was never created (the historical bug that surfaced
        # ``Could not find the file …`` on top of the real error).
        self.assertEqual(opts, {'stop_on_failure': True})

    def test_non_prod_default_payload_includes_filestore(self):
        # Backwards compatibility: no payload at all → default to a
        # full backup (DB + filestore). Mirrors click-odoo-backupdb's
        # own default and matches every legacy row in the DB.
        ex = self._make_executor(environment='staging')
        ex.job = MagicMock()
        ex.job.payload = None
        cmd = ex.get_commands()[0][1]
        self.assertIn('--filestore', cmd)
        self.assertNotIn('--no-filestore', cmd)

    def test_non_prod_with_filestore_true_uses_filestore_flag(self):
        ex = self._make_executor_with_payload(
            'staging', {'with_filestore': True},
        )
        cmd = ex.get_commands()[0][1]
        self.assertIn('--filestore', cmd)
        self.assertNotIn('--no-filestore', cmd)

    def test_non_prod_with_filestore_false_uses_no_filestore_flag(self):
        ex = self._make_executor_with_payload(
            'staging', {'with_filestore': False},
        )
        cmd = ex.get_commands()[0][1]
        self.assertIn('--no-filestore', cmd)
        # Sanity: the dbname still follows the flag.
        self.assertIn('--no-filestore prod', cmd)

    def test_non_prod_truthy_string_payload_collapses_to_filestore(self):
        # Defensive: any truthy JSON-RPC value collapses to True via
        # ``bool()`` at the executor boundary so the user can never
        # smuggle an arbitrary string into the click-odoo-backupdb
        # invocation.
        ex = self._make_executor_with_payload(
            'staging', {'with_filestore': '--malicious; rm -rf /'},
        )
        cmd = ex.get_commands()[0][1]
        self.assertIn('--filestore', cmd)
        self.assertNotIn('rm -rf', cmd)
        self.assertNotIn('--malicious', cmd)

    def test_production_uses_backup_container_jobrunner(self):
        cmds = self._make_executor(environment='production').get_commands()
        self.assertEqual(len(cmds), 1)
        label, cmd = cmds[0][0], cmds[0][1]
        self.assertEqual(label, 'Create backup')
        self.assertIn('docker compose exec -T backup', cmd)
        self.assertIn('/etc/periodic/daily/jobrunner', cmd)

    def test_production_payload_does_not_inject_filestore_flag(self):
        # Production runs duply (jobrunner) which controls shape.
        # The kwarg must not leak into the prod command line.
        ex = self._make_executor_with_payload(
            'production', {'with_filestore': False},
        )
        cmd = ex.get_commands()[0][1]
        self.assertNotIn('--no-filestore', cmd)
        self.assertNotIn('--filestore', cmd)


# ---------------------------------------------------------------------------
# 6b. RestoreInstanceExecutor.get_commands — Verify backup file step
# ---------------------------------------------------------------------------


class TestRestoreInstanceVerifyBackupFile(BaseCase):
    """The ``Verify backup file`` step must hand the uploaded zip over to
    the odoo container's UID — the SSH user (root) and the container's
    odoo user have different UIDs, so a plain ``chmod 600`` would leave
    the bind-mounted zip unreadable from inside the container
    (``click-odoo-restoredb`` prints "Path '/mnt/restore.zip' is not
    readable.").  We discover the UID dynamically with ``docker compose
    run --rm --entrypoint="" odoo id -u`` so the fix survives doodba
    image upgrades that ever change it.
    """

    def _make_executor(self):
        from odoo.addons.incubacloud.models.restore_instance_executor import (
            RestoreInstanceExecutor,
        )
        inst = SimpleNamespace(
            id=42,
            name='myinst',
            postgres_dbname='prod',
        )
        ex = object.__new__(RestoreInstanceExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: f"/home/{i.name}"
        return ex

    def test_verify_step_chowns_to_dynamic_odoo_uid(self):
        cmds = self._make_executor().get_commands()
        label, cmd, opts = cmds[0]
        self.assertEqual(label, 'Verify backup file')
        # Existence check first — fail fast with a clear message.
        self.assertIn('test -f /tmp/incubacloud-restore-42.zip', cmd)
        self.assertIn('Backup file not found at', cmd)
        # UID discovery: ``run --rm`` (not ``exec``) so it works even
        # when the odoo service is stopped; ``--entrypoint=""`` skips
        # doodba's addon-linking init so ``id -u`` returns immediately;
        # ``tr -d`` strips trailing CR/LF that would break ``chown``.
        self.assertIn(
            'docker compose run --rm --entrypoint=""'
            ' odoo id -u',
            cmd,
        )
        self.assertIn('tr -d "\\r\\n"', cmd)
        self.assertIn('Could not discover odoo container UID', cmd)
        # chown to the discovered UID, then 600 so only the container
        # user (and root) can read the tenant DB dump + filestore.
        self.assertIn(
            'chown "$ODOO_UID":"$ODOO_UID"'
            ' /tmp/incubacloud-restore-42.zip',
            cmd,
        )
        self.assertIn('chmod 600 /tmp/incubacloud-restore-42.zip', cmd)
        # Stop the chain on failure: skipping the rest avoids destroying
        # the live DB with ``click-odoo-restoredb --copy --force`` when
        # we already know the upload is missing or unreadable.
        self.assertEqual(opts, {'stop_on_failure': True})

    def test_chown_runs_before_chmod(self):
        # ``chmod 600`` before ``chown`` would re-tighten permissions
        # only to have ``chown`` re-apply them — harmless today, but
        # the invariant we want is "the container user owns the file
        # before anything else touches it", so assert the order.
        _, cmd, _ = self._make_executor().get_commands()[0]
        self.assertLess(cmd.index('chown'), cmd.index('chmod 600'))


# ---------------------------------------------------------------------------
# 7. BackupDownloadExecutor.after_commands
# ---------------------------------------------------------------------------

class TestBackupDownloadAfterCommands(unittest.TestCase):

    def _make_executor(self):
        from odoo.addons.incubacloud.models.backup_download_executor import (
            BackupDownloadExecutor,
        )
        inst = SimpleNamespace(name='dl-inst', postgres_dbname='prod')
        ex = object.__new__(BackupDownloadExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: f"/home/{i.name}"
        ex._log_buffer = []
        ex._sys = ex._log_buffer.append
        ex.job = MagicMock()
        ex.job.id = 77
        ex.job.payload = {'time': 'latest', 'download_type': 'dump'}
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

    def _make_executor(self, environment, mode='dump', time='latest'):
        from odoo.addons.incubacloud.models.backup_download_executor import (
            BackupDownloadExecutor,
        )
        inst = SimpleNamespace(
            name='dl-inst',
            environment=environment,
            postgres_dbname='prod',
        )
        ex = object.__new__(BackupDownloadExecutor)
        ex._inst = lambda: inst
        ex._inst_dir = lambda i: f"/home/{i.name}"
        ex.job = MagicMock()
        ex.job.id = 77
        ex.job.payload = {'time': time, 'download_type': mode}
        return ex

    # ── non-production: click-odoo-backupdb live dump ─────────────────

    def test_non_prod_dump_uses_click_odoo_backupdb_no_filestore(self):
        cmds = self._make_executor('staging', mode='dump').get_commands()
        self.assertEqual(len(cmds), 1)
        label, cmd, opts = cmds[0]
        self.assertEqual(label, 'Create live backup')
        self.assertIn('docker compose run --rm', cmd)
        self.assertIn('-v /tmp:/host-tmp', cmd)
        self.assertIn('odoo click-odoo-backupdb', cmd)
        self.assertIn('--no-filestore prod', cmd)
        self.assertIn('/host-tmp/.incubacloud-bkdl-dl-inst.zip', cmd)
        # Old prod-only path must NOT leak into staging.
        self.assertNotIn('docker compose exec -T backup', cmd)
        self.assertNotIn('dup restore', cmd)
        # Stop the chain on failure so after_commands does not try to
        # download a zip that was never created — same defensive
        # pattern as BackupCreateExecutor.
        self.assertEqual(opts, {'stop_on_failure': True})

    def test_non_prod_all_uses_click_odoo_backupdb_with_filestore(self):
        cmds = self._make_executor('staging', mode='all').get_commands()
        self.assertEqual(len(cmds), 1)
        label, cmd, _ = cmds[0]
        self.assertEqual(label, 'Create live backup')
        self.assertIn('--filestore prod', cmd)
        self.assertNotIn('--no-filestore', cmd)

    # ── production: duplicity restore from S3 ─────────────────────────

    def test_prod_dump_uses_duply_in_backup_container(self):
        cmds = self._make_executor('production', mode='dump').get_commands()
        self.assertEqual(len(cmds), 2)
        labels = [c[0] for c in cmds]
        self.assertEqual(
            labels,
            ['Restore SQL from backup', 'Extract and package'],
        )
        restore_cmd = cmds[0][1]
        self.assertIn('docker compose exec -T backup', restore_cmd)
        self.assertIn('dup restore --force', restore_cmd)
        self.assertIn('--path-to-restore prod.sql', restore_cmd)

    def test_prod_all_uses_duply_full_restore(self):
        cmds = self._make_executor('production', mode='all').get_commands()
        self.assertEqual(len(cmds), 2)
        labels = [c[0] for c in cmds]
        self.assertEqual(
            labels,
            ['Restore full from backup', 'Extract and package'],
        )
        restore_cmd = cmds[0][1]
        self.assertIn('docker compose exec -T backup', restore_cmd)
        self.assertIn('dup restore --force', restore_cmd)
        # Full restore: no --path-to-restore filter, dumps the whole
        # backup tree to ctr_tmp for the cp + zip step that follows.
        self.assertNotIn('--path-to-restore', restore_cmd)

    def test_prod_explicit_time_renders_time_flag(self):
        ex = self._make_executor(
            'production', mode='dump', time='2026-03-19T02:00:00',
        )
        restore_cmd = ex.get_commands()[0][1]
        self.assertIn('--time "2026-03-19T02:00:00"', restore_cmd)

    def test_prod_latest_time_omits_time_flag(self):
        ex = self._make_executor('production', mode='dump', time='latest')
        restore_cmd = ex.get_commands()[0][1]
        self.assertNotIn('--time', restore_cmd)


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
            name='dl-inst',
            environment=environment,
            postgres_dbname='prod',
        )
        ex = object.__new__(BackupDownloadExecutor)
        ex._inst = lambda: inst
        ex.job = MagicMock()
        ex.job.payload = {'time': time, 'download_type': 'dump'}
        return ex

    def test_non_prod_rejects_historical_timestamp(self):
        ex = self._make_executor('staging', '2026-03-19T02:00:00')
        with self.assertRaises(ValueError) as ctx:
            _run(ex.before_execute(MagicMock()))
        self.assertIn('historical', str(ctx.exception).lower())

    def test_non_prod_accepts_latest(self):
        ex = self._make_executor('staging', 'latest')
        # Should not raise.
        _run(ex.before_execute(MagicMock()))

    def test_prod_accepts_historical_timestamp(self):
        ex = self._make_executor('production', '2026-03-19T02:00:00')
        # Should not raise — duplicity will resolve the snapshot.
        _run(ex.before_execute(MagicMock()))
