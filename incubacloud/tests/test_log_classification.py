"""Tests for log chunk classification by content (not by SSH channel).

The executor reclassifies stream output: stderr is no longer a synonym
for "error". Lines are tagged ``stderr`` (rendered red ``[err]``) only
when they match a whitelist of error-ish patterns; everything else
becomes ``stdout`` (gray ``[out]``) regardless of the channel it came
from. A `[sys]` breadcrumb is emitted on any non-zero exit code so true
failures stay visible even when their output doesn't match a pattern.
"""

import asyncio
from types import SimpleNamespace

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models.abstract_executor import (
    _looks_like_error,
)


def _drain(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_executor():
    """Bare-bones executor with just the buffer plumbing wired up.

    Uses a concrete subclass because ``AbstractExecutor`` is an ABC and
    ``object.__new__`` still enforces the abstract-method check. Any
    concrete executor works — pick a small one with no side effects in
    ``__init__``.
    """
    from odoo.addons.incubacloud.models.backup_download_neutralized_executor \
        import BackupDownloadNeutralizedExecutor
    ex = object.__new__(BackupDownloadNeutralizedExecutor)
    ex._log_buffer = []
    return ex


class TestLooksLikeError(BaseCase):
    """Pure-function tests on the pattern whitelist."""

    def test_python_traceback_start(self):
        self.assertTrue(_looks_like_error(
            "Traceback (most recent call last):"
        ))

    def test_python_traceback_frame(self):
        self.assertTrue(_looks_like_error(
            '  File "/opt/odoo/x.py", line 42, in foo'
        ))

    def test_odoo_error_log_level(self):
        self.assertTrue(_looks_like_error(
            "2026-01-01 12:00:00,000 1 ERROR devel odoo.foo: bar"
        ))

    def test_odoo_fatal_log_level(self):
        self.assertTrue(_looks_like_error(
            "2026-01-01 12:00:00,000 1 FATAL devel odoo.foo: bar"
        ))

    def test_odoo_critical_log_level(self):
        self.assertTrue(_looks_like_error(
            "2026-01-01 12:00:00,000 1 CRITICAL devel odoo.foo: bar"
        ))

    def test_odoo_warning_log_level_is_benign(self):
        self.assertFalse(_looks_like_error(
            "2026-01-01 12:00:00,000 1 WARNING devel odoo.foo: bar"
        ))

    def test_odoo_info_log_level_is_benign(self):
        self.assertFalse(_looks_like_error(
            "2026-01-01 12:00:00,000 1 INFO devel odoo.foo: bar"
        ))

    def test_rm_cannot_remove(self):
        self.assertTrue(_looks_like_error(
            "rm: cannot remove '/tmp/x.zip': Operation not permitted"
        ))

    def test_cp_cannot(self):
        self.assertTrue(_looks_like_error(
            "cp: cannot stat '/tmp/missing': No such file or directory"
        ))

    def test_permission_denied_suffix(self):
        self.assertTrue(_looks_like_error(
            "rmdir: failed to remove '/foo': Permission denied"
        ))

    def test_docker_daemon_error(self):
        self.assertTrue(_looks_like_error(
            "Error response from daemon: pull access denied for foo"
        ))

    def test_apt_error(self):
        self.assertTrue(_looks_like_error(
            "E: Unable to locate package foo"
        ))

    def test_docker_compose_cp_progress_is_benign(self):
        self.assertFalse(_looks_like_error(
            " incubacloud-prod-odoo-1 Copying"
            " /tmp/.incubacloud-bkneu-1/src.zip to"
            " incubacloud-prod-odoo-1:/tmp/bkneu-src-1.zip"
        ))
        self.assertFalse(_looks_like_error(
            " incubacloud-prod-odoo-1 Copied"
            " /tmp/.incubacloud-bkneu-1/src.zip to"
            " incubacloud-prod-odoo-1:/tmp/bkneu-src-1.zip"
        ))

    def test_duplicity_progress_is_benign(self):
        for line in (
            "Local and Remote metadata are synchronized, no sync needed.",
            "Last full backup date: Sun May 10 01:00:06 2026",
            "Single-destination mode enabled",
        ):
            self.assertFalse(_looks_like_error(line))

    def test_random_neutral_text_is_benign(self):
        self.assertFalse(_looks_like_error("hello world"))
        self.assertFalse(_looks_like_error("adding: dump.sql (deflated 87%)"))


class TestStreamCallbacksClassifyByContent(BaseCase):
    """on_stdout / on_stderr both classify by content, not channel."""

    def test_stderr_with_benign_content_becomes_stdout(self):
        ex = _make_executor()
        _drain(ex.on_stderr(
            " prod-odoo-1 Copying /tmp/a to b"
        ))
        self.assertEqual(len(ex._log_buffer), 1)
        _line, source = ex._log_buffer[0]
        self.assertEqual(source, "stdout")

    def test_stderr_with_warning_log_becomes_stdout(self):
        ex = _make_executor()
        _drain(ex.on_stderr(
            "2026-05-10 15:04:45,065 89 WARNING ?"
            " odoo.tools.config: option smtp_user reads 'false'"
        ))
        _line, source = ex._log_buffer[0]
        self.assertEqual(source, "stdout")

    def test_stderr_with_real_error_stays_stderr(self):
        ex = _make_executor()
        _drain(ex.on_stderr(
            "rm: cannot remove '/tmp/x.zip': Operation not permitted"
        ))
        _line, source = ex._log_buffer[0]
        self.assertEqual(source, "stderr")

    def test_stdout_with_traceback_becomes_stderr(self):
        # A tool that writes its python traceback to stdout (rare but
        # happens, e.g. `python -c '...' 2>&1`) gets correctly flagged.
        ex = _make_executor()
        _drain(ex.on_stdout("Traceback (most recent call last):"))
        _line, source = ex._log_buffer[0]
        self.assertEqual(source, "stderr")

    def test_stdout_with_neutral_content_stays_stdout(self):
        ex = _make_executor()
        _drain(ex.on_stdout("adding: dump.sql (deflated 87%)"))
        _line, source = ex._log_buffer[0]
        self.assertEqual(source, "stdout")


class TestSysMessagesUnchanged(BaseCase):
    """_sys() always emits 'system', regardless of content."""

    def test_sys_is_always_system(self):
        ex = _make_executor()
        ex._sys("Running: foo")
        ex._sys("✗ ERROR-looking but still a system message")
        for _line, source in ex._log_buffer:
            self.assertEqual(source, "system")
