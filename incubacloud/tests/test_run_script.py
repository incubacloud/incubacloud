"""
Tier 1 — Pure-Python unit tests for AbstractExecutor's versioned-script
helpers (``run_script`` / ``_upload_scripts`` / ``_discard_staging``).

These helpers are the seam the whole Phase 3 migration hangs on: every
executor that stops interpolating bash into an f-string calls
``run_script()`` instead. Argument quoting, overlay resolution and the
upload/cleanup lifecycle are therefore tested here directly, without a
host or the ORM (the executor is built with ``object.__new__`` like the
other Tier 1 executor tests).
"""
import asyncio
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models import abstract_executor
from odoo.addons.incubacloud.models.abstract_executor import (
    JOB_DIR_PREFIX,
    AbstractExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

# Shipped by ``incubacloud/scripts/`` — the shared library every
# operation script sources. Used here as a real overlay entry so the
# tests exercise actual files instead of a fixture.
SHARED_LIB = "lib/common.sh"


class _ScriptExecutor(AbstractExecutor):
    """Test subject. No ``_job_type`` → never enters the executor registry."""

    def get_commands(self):
        return []


def _make_executor(job_id=42):
    """Build an executor without ``__init__`` (no SSH, no ORM)."""
    executor = object.__new__(_ScriptExecutor)
    executor.job = SimpleNamespace(id=job_id)
    executor._log_buffer = []
    executor._scripts_requested = False
    executor._scripts_uploaded = False
    executor._script_overlay_cache = None
    return executor


def _make_transport(exit_status=0, tar_status=None):
    """Return a transport mock spec'd against the real SSH transport.

    :param int exit_status: what ``run`` answers
    :param int tar_status: what ``run_with_stdin`` answers; None for a
        transport without it, which uploads file by file
    """
    transport = MagicMock(spec=SSHTransport)
    transport.run.return_value = CommandResult(stdout="", exit_status=exit_status)
    if tar_status is None:
        transport.run_with_stdin.side_effect = NotImplementedError
    else:
        transport.run_with_stdin.return_value = CommandResult(
            stdout="tar: not found" if tar_status else "", exit_status=tar_status,
        )
    return transport


class TestScriptOverlay(BaseCase):

    def setUp(self):
        self.executor = _make_executor()

    def test_overlay_contains_the_shared_library(self):
        self.assertIn(SHARED_LIB, self.executor._script_overlay())

    def test_overlay_maps_to_existing_local_files(self):
        for local in self.executor._script_overlay().values():
            self.assertTrue(Path(local).is_file(), f"missing script: {local}")

    def test_overlay_keys_are_relative_posix_paths(self):
        for rel in self.executor._script_overlay():
            self.assertFalse(rel.startswith("/"), rel)
            self.assertNotIn("\\", rel)

    def test_overlay_is_cached_per_instance(self):
        first = self.executor._script_overlay()
        self.assertIs(first, self.executor._script_overlay())

    def test_addons_of_a_core_executor(self):
        self.assertEqual(self.executor._addon_chain(), ["incubacloud"])

    def test_addons_are_ordered_most_derived_first(self):
        """A layered subclass must see its own scripts before core's.

        That ordering is what lets ``_script_overlay`` shadow a core
        script with a same-named one shipped by the subclass's addon.
        """

        class _LayerExecutor(_ScriptExecutor):
            pass

        _LayerExecutor.__module__ = (
            "odoo.addons.acme_layer.models.layer_deploy_executor"
        )
        self.assertEqual(
            _LayerExecutor._addon_chain(),
            ["acme_layer", "incubacloud"],
        )


class TestRunScript(BaseCase):

    def setUp(self):
        self.executor = _make_executor(job_id=7)
        self.root = self.executor._script_root()

    def test_root_is_in_the_job_s_own_directory(self):
        """Not a name another account could guess: the job's directory,
        random and the job's own, and another job's is another."""
        self.assertEqual(self.root, f"{self.executor._job_dir()}/scripts")
        self.assertTrue(self.root.startswith(JOB_DIR_PREFIX))
        self.assertNotEqual(_make_executor(job_id=7)._script_root(), self.root)

    def test_returns_a_bash_command_for_the_remote_copy(self):
        self.assertEqual(
            self.executor.run_script(SHARED_LIB),
            f"bash {self.root}/{SHARED_LIB}",
        )

    def test_appends_shell_quoted_arguments(self):
        command = self.executor.run_script(SHARED_LIB, ["/srv/my app", "plain"])
        self.assertEqual(
            command,
            f"bash {self.root}/{SHARED_LIB} '/srv/my app' plain",
        )

    def test_neutralises_argument_injection(self):
        """An argument can never break out into a second command."""
        command = self.executor.run_script(SHARED_LIB, ["; rm -rf /"])
        self.assertEqual(command, f"bash {self.root}/{SHARED_LIB} '; rm -rf /'")

    def test_stringifies_non_string_arguments(self):
        self.assertEqual(
            self.executor.run_script(SHARED_LIB, [42, True]),
            f"bash {self.root}/{SHARED_LIB} 42 True",
        )

    def test_requests_upload_only_once_used(self):
        self.assertFalse(self.executor._scripts_requested)
        self.executor.run_script(SHARED_LIB)
        self.assertTrue(self.executor._scripts_requested)

    def test_unknown_script_fails_fast_and_queues_nothing(self):
        with self.assertRaises(FileNotFoundError):
            self.executor.run_script("does_not_exist.sh")
        self.assertFalse(self.executor._scripts_requested)


class TestScriptUpload(BaseCase):

    def setUp(self):
        self.executor = _make_executor(job_id=7)
        self.root = self.executor._script_root()
        self.transport = _make_transport()

    def test_does_nothing_when_no_script_was_requested(self):
        asyncio.run(self.executor._upload_scripts(self.transport))
        self.transport.run.assert_not_called()
        self.transport.upload_text_files.assert_not_called()

    def test_creates_the_remote_directory_with_0700(self):
        """In the job's directory, created first: a fresh one, 0700."""
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._upload_scripts(self.transport))
        first, command = (c.args[0] for c in self.transport.run.call_args_list)
        self.assertEqual(first, f"mkdir -m 700 {self.executor._job_dir()}")
        self.assertTrue(command.startswith("mkdir -p -m 700 "), command)
        # Every directory the overlay needs is created up front, so an
        # SFTP write never lands in a missing folder.
        self.assertIn(f"{self.root}/lib", command)

    def test_uploads_the_whole_overlay_not_just_the_named_script(self):
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._upload_scripts(self.transport))
        files = self.transport.upload_text_files.call_args.args[0]
        self.assertEqual(
            set(files),
            {f"{self.root}/{rel}" for rel in self.executor._script_overlay()},
        )

    def test_uploads_the_real_file_content(self):
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._upload_scripts(self.transport))
        files = self.transport.upload_text_files.call_args.args[0]
        self.assertIn("ic_die()", files[f"{self.root}/{SHARED_LIB}"])

    def test_raises_when_the_remote_directory_cannot_be_created(self):
        self.executor.run_script(SHARED_LIB)
        self.transport.run.return_value = CommandResult(stdout="", exit_status=1)
        with self.assertRaises(RuntimeError):
            asyncio.run(self.executor._upload_scripts(self.transport))
        self.transport.upload_text_files.assert_not_called()


class TestScriptUploadAsTar(BaseCase):
    """The overlay goes up in one exchange: a tar on a command's input."""

    def setUp(self):
        self.executor = _make_executor(job_id=7)
        self.root = self.executor._script_root()
        self.executor.run_script(SHARED_LIB)

    def _upload(self, tar_status):
        """Upload the overlay through a host whose tar exits *tar_status*.

        :return: the transport mock
        """
        transport = _make_transport(tar_status=tar_status)
        asyncio.run(self.executor._upload_scripts(transport))
        return transport

    def _archive(self, transport):
        """Return the tar the upload sent, opened.

        :rtype: tarfile.TarFile
        """
        data = transport.run_with_stdin.call_args.args[1]
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:")

    def test_one_command_unpacks_the_whole_overlay(self):
        transport = self._upload(0)
        transport.run_with_stdin.assert_awaited_once()
        command = transport.run_with_stdin.call_args.args[0]
        self.assertEqual(command, (
            f"mkdir -p -m 700 {self.root} && tar -xmf - -C {self.root}"
        ))
        self.assertEqual(
            transport.run_with_stdin.call_args.kwargs["timeout"],
            abstract_executor.SCRIPTS_TAR_TIMEOUT_S,
        )
        transport.upload_text_files.assert_not_called()
        # Only the job's own directory is made apart.
        transport.run.assert_awaited_once_with(
            f"mkdir -m 700 {self.executor._job_dir()}",
        )
        self.assertTrue(self.executor._scripts_uploaded)

    def test_the_tar_holds_every_script_private(self):
        archive = self._archive(self._upload(0))
        members = {m.name: m for m in archive.getmembers()}
        files = {n for n, m in members.items() if m.isfile()}
        self.assertEqual(files, set(self.executor._script_overlay()))
        self.assertTrue(members["lib"].isdir())
        for member in members.values():
            with self.subTest(member=member.name):
                self.assertEqual(member.mode, 0o700 if member.isdir() else 0o600)
                self.assertEqual((member.uid, member.gid), (0, 0))
                self.assertEqual((member.uname, member.gname), ("", ""))
                self.assertFalse(member.pax_headers)
        local = Path(self.executor._script_overlay()[SHARED_LIB])
        self.assertEqual(
            archive.extractfile(SHARED_LIB).read(), local.read_bytes(),
        )

    def test_a_tar_that_fails_falls_back_to_sftp(self):
        """Any exit but 0: a host where the tar fails still runs jobs."""
        transport = self._upload(127)
        files = transport.upload_text_files.call_args.args[0]
        self.assertEqual(
            set(files),
            {f"{self.root}/{rel}" for rel in self.executor._script_overlay()},
        )
        self.assertTrue(self.executor._scripts_uploaded)
        self.assertTrue(any(
            "one by one" in str(entry) for entry in self.executor._log_buffer
        ))

    def test_a_transport_without_it_uploads_file_by_file(self):
        transport = self._upload(None)
        transport.upload_text_files.assert_awaited_once()

    def test_a_lost_connection_is_not_a_failure_of_the_tar(self):
        transport = _make_transport()
        transport.run_with_stdin.side_effect = ConnectionResetError("gone")
        with self.assertRaises(ConnectionResetError):
            asyncio.run(self.executor._upload_scripts(transport))
        transport.upload_text_files.assert_not_called()
        self.assertFalse(self.executor._scripts_uploaded)


class TestScriptCleanup(BaseCase):

    def setUp(self):
        self.executor = _make_executor(job_id=7)
        self.transport = _make_transport()

    def test_does_nothing_when_nothing_was_uploaded(self):
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._discard_staging(self.transport))
        self.transport.run.assert_not_called()

    def test_removes_the_job_directory(self):
        """The scripts go with the job's directory, as one."""
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._upload_scripts(self.transport))
        self.transport.run.reset_mock()
        asyncio.run(self.executor._discard_staging(self.transport))
        self.transport.run.assert_called_once_with(
            f"rm -rf {self.executor._job_dir()}",
        )

    def test_never_masks_the_error_it_runs_after(self):
        """Cleanup lives in a ``finally``: it must swallow its own errors."""
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._upload_scripts(self.transport))
        self.transport.run.side_effect = OSError("connection lost")
        asyncio.run(self.executor._discard_staging(self.transport))


class TestScriptEnvironment(BaseCase):
    """``env``: what a script's whole run shares, such as the series' tool."""

    def setUp(self):
        self.executor = _make_executor(job_id=7)
        self.root = self.executor._script_root()

    def test_the_environment_precedes_the_command(self):
        command = self.executor.run_script(
            SHARED_LIB, ["live-dump"], env={"IC_DB_TOOL": "odoo-db"},
        )
        self.assertEqual(
            command, f"IC_DB_TOOL=odoo-db bash {self.root}/{SHARED_LIB} live-dump",
        )

    def test_a_value_is_shell_quoted(self):
        command = self.executor.run_script(SHARED_LIB, env={"IC_X": "a b; rm -rf /"})
        self.assertEqual(
            command, f"IC_X='a b; rm -rf /' bash {self.root}/{SHARED_LIB}",
        )

    def test_a_name_a_shell_would_not_take_is_refused(self):
        for name in ("ic-db", "1X", "IC X", "IC_X=1"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.executor.run_script(SHARED_LIB, env={name: "x"})

    def test_no_environment_changes_nothing(self):
        self.assertEqual(
            self.executor.run_script(SHARED_LIB, env={}),
            f"bash {self.root}/{SHARED_LIB}",
        )
