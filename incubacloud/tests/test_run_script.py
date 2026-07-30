"""
Tier 1 — Pure-Python unit tests for AbstractExecutor's versioned-script
helpers (``run_script`` / ``_upload_scripts`` / ``_cleanup_scripts``).

These helpers are the seam the whole Phase 3 migration hangs on: every
executor that stops interpolating bash into an f-string calls
``run_script()`` instead. Argument quoting, overlay resolution and the
upload/cleanup lifecycle are therefore tested here directly, without a
host or the ORM (the executor is built with ``object.__new__`` like the
other Tier 1 executor tests).
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models.abstract_executor import (
    SCRIPT_REMOTE_ROOT,
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


def _make_transport(exit_status=0):
    """Return a transport mock spec'd against the real SSH transport."""
    transport = MagicMock(spec=SSHTransport)
    transport.run.return_value = CommandResult(stdout="", exit_status=exit_status)
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
        """A saas subclass must see its own scripts before core's.

        That ordering is what lets ``_script_overlay`` shadow a core
        script with a same-named one shipped by the subclass's addon.
        """

        class _SaasExecutor(_ScriptExecutor):
            pass

        _SaasExecutor.__module__ = (
            "odoo.addons.incubacloud_saas_manager.models.tenant_deploy_executor"
        )
        self.assertEqual(
            _SaasExecutor._addon_chain(),
            ["incubacloud_saas_manager", "incubacloud"],
        )


class TestRunScript(BaseCase):

    def setUp(self):
        self.executor = _make_executor(job_id=7)
        self.root = f"{SCRIPT_REMOTE_ROOT}-7"

    def test_root_is_scoped_to_the_job(self):
        self.assertEqual(self.executor._script_root(), self.root)
        self.assertEqual(_make_executor(job_id=8)._script_root(), f"{SCRIPT_REMOTE_ROOT}-8")

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
        self.root = f"{SCRIPT_REMOTE_ROOT}-7"
        self.transport = _make_transport()

    def test_does_nothing_when_no_script_was_requested(self):
        asyncio.run(self.executor._upload_scripts(self.transport))
        self.transport.run.assert_not_called()
        self.transport.upload_text_files.assert_not_called()

    def test_recreates_the_remote_directory_with_0700(self):
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._upload_scripts(self.transport))
        command = self.transport.run.call_args.args[0]
        self.assertTrue(
            command.startswith(f"rm -rf {self.root} && mkdir -p -m 700 "),
            command,
        )
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


class TestScriptCleanup(BaseCase):

    def setUp(self):
        self.executor = _make_executor(job_id=7)
        self.root = f"{SCRIPT_REMOTE_ROOT}-7"
        self.transport = _make_transport()

    def test_does_nothing_when_no_script_was_requested(self):
        asyncio.run(self.executor._cleanup_scripts(self.transport))
        self.transport.run.assert_not_called()

    def test_removes_the_job_directory(self):
        self.executor.run_script(SHARED_LIB)
        asyncio.run(self.executor._cleanup_scripts(self.transport))
        self.transport.run.assert_called_once_with(f"rm -rf {self.root}")

    def test_never_masks_the_error_it_runs_after(self):
        """Cleanup lives in a ``finally``: it must swallow its own errors."""
        self.executor.run_script(SHARED_LIB)
        self.transport.run.side_effect = OSError("connection lost")
        asyncio.run(self.executor._cleanup_scripts(self.transport))
