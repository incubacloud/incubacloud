"""The log archive commands, executed against real files.

The builders in ``_helpers`` are also tested for what they *say* —
the flags and guards present in the text. This module runs them. The
one hole found in this feature (a symlink planted in ``logs/`` from
inside the container turning the viewer into a host file reader) was
invisible to a string check and only showed up by executing the
command against a planted link, so that is how it is guarded here:
a temporary instance directory with a live file, a plain day, a
compressed day and a link to a canary outside ``logs/``, and every
command run through ``sh`` exactly as the host would run it.
"""
import gzip
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.controllers._data_load._helpers import (
    log_archive_download_command,
    log_archive_list_command,
    log_archive_search_command,
    odoo_log_live_command,
    odoo_log_read_command,
)

LIVE = (
    "2026-08-19 09:00:00,000 1 INFO db odoo.modules: live one\n"
    "2026-08-19 09:00:01,000 1 INFO db odoo.modules: live two\n"
    "2026-08-19 09:00:02,000 1 ERROR db odoo.sql_db: live three\n"
)
YESTERDAY = (
    "2026-08-18 10:00:00,000 1 INFO db odoo.modules: yesterday one\n"
    "2026-08-18 10:00:01,000 1 ERROR db odoo.sql_db: needle in yesterday\n"
)
OLDER = (
    "2026-08-17 11:00:00,000 1 INFO db odoo.modules: older one\n"
    "2026-08-17 11:00:01,000 1 ERROR db odoo.sql_db: needle in older\n"
    "2026-08-17 11:00:02,000 1 ERROR db odoo.sql_db: needle again\n"
)
#: What the planted link points at. It mentions the sweep's term on
#: purpose: a sweep that followed the link would count it.
CANARY = "HOST-SECRET needle\n"

PLAIN_DAY = "odoo.log.2026-08-18"
GZ_DAY = "odoo.log.2026-08-17.gz"
LINKED_DAY = "odoo.log.2026-08-16"


class _ArchiveOnDisk(BaseCase):
    """A temporary instance directory shaped like the host's."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="ic-log-cmd-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.logs = Path(self.root) / "logs"
        self.logs.mkdir()
        self._write("odoo.log", LIVE)
        self._write(PLAIN_DAY, YESTERDAY)
        self._write_gz(GZ_DAY, OLDER)
        # Date the files like logrotate leaves them, newest first: the
        # sweep orders by mtime, and three files born in the same
        # second would otherwise be ordered by chance.
        now = time.time()
        for name, age_days in (("odoo.log", 0), (PLAIN_DAY, 1), (GZ_DAY, 2)):
            when = now - age_days * 86400
            os.utime(self.logs / name, (when, when))
        self.canary = Path(self.root) / "CANARY"
        self.canary.write_text(CANARY, encoding="utf-8")
        (self.logs / LINKED_DAY).symlink_to(self.canary)

    def _write(self, name, text):
        """Write a plain log file into ``logs/``."""
        (self.logs / name).write_text(text, encoding="utf-8")

    def _write_gz(self, name, text):
        """Write a gzip-compressed log file into ``logs/``."""
        with gzip.open(self.logs / name, "wt", encoding="utf-8") as fh:
            fh.write(text)

    def _run(self, command):
        """Run *command* through ``sh`` as the host would; return stdout bytes."""
        proc = subprocess.run(
            ["sh", "-c", command],
            capture_output=True, timeout=60, check=False,
        )
        return proc.stdout

    def _lines(self, command):
        """Run *command* and return its stdout as a list of text lines."""
        return self._run(command).decode("utf-8", "replace").splitlines()


class TestListingOnDisk(_ArchiveOnDisk):

    def test_the_listing_reports_the_real_files_and_not_the_link(self):
        rows = self._lines(log_archive_list_command(self.root))
        names = {row.split("|")[0] for row in rows}
        self.assertEqual(names, {"odoo.log", PLAIN_DAY, GZ_DAY})
        self.assertNotIn(LINKED_DAY, names)

    def test_the_listing_carries_size_and_mtime(self):
        rows = self._lines(log_archive_list_command(self.root))
        by_name = {row.split("|")[0]: row.split("|")[1:] for row in rows}
        size, mtime = by_name[PLAIN_DAY]
        self.assertEqual(int(size), len(YESTERDAY.encode()))
        self.assertGreater(int(mtime), 0)

    def test_an_instance_without_logs_lists_nothing_and_does_not_fail(self):
        shutil.rmtree(self.logs)
        proc = subprocess.run(
            ["sh", "-c", log_archive_list_command(self.root)],
            capture_output=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")


class TestReadingOnDisk(_ArchiveOnDisk):

    def test_reading_a_plain_day_returns_its_lines(self):
        lines = self._lines(odoo_log_read_command(self.root, PLAIN_DAY, 100))
        self.assertEqual(lines, YESTERDAY.splitlines())

    def test_reading_a_compressed_day_returns_its_lines(self):
        lines = self._lines(odoo_log_read_command(self.root, GZ_DAY, 100))
        self.assertEqual(lines, OLDER.splitlines())

    def test_the_tail_is_bounded(self):
        lines = self._lines(odoo_log_read_command(self.root, GZ_DAY, 1))
        self.assertEqual(lines, OLDER.splitlines()[-1:])

    def test_the_filter_runs_on_the_host_and_keeps_matching_lines_only(self):
        lines = self._lines(
            odoo_log_read_command(self.root, GZ_DAY, 100, search="needle again"),
        )
        self.assertEqual(lines, [OLDER.splitlines()[-1]])

    def test_the_filter_is_a_fixed_string_not_a_pattern(self):
        """A term with regex metacharacters must not match everything."""
        lines = self._lines(
            odoo_log_read_command(self.root, GZ_DAY, 100, search=".*"),
        )
        self.assertEqual(lines, [])

    def test_reading_the_planted_link_returns_nothing(self):
        out = self._run(odoo_log_read_command(self.root, LINKED_DAY, 100))
        self.assertEqual(out, b"")
        self.assertNotIn(b"HOST-SECRET", out)

    def test_a_missing_day_returns_nothing_and_does_not_fail(self):
        proc = subprocess.run(
            ["sh", "-c", odoo_log_read_command(self.root, "odoo.log.2000-01-01", 10)],
            capture_output=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")

    def test_the_live_tail_reads_the_file(self):
        lines = self._lines(odoo_log_live_command(self.root, "prod.yaml", 2))
        self.assertEqual(lines, LIVE.splitlines()[-2:])

    def test_the_live_tail_refuses_a_link(self):
        live = self.logs / "odoo.log"
        live.unlink()
        live.symlink_to(self.canary)
        out = self._run(odoo_log_live_command(self.root, "prod.yaml", 10))
        # Whatever the fallback prints (there is no docker here), the
        # one thing it must never print is the file behind the link.
        self.assertNotIn(b"HOST-SECRET", out)


class TestReadingADayCompressedMeanwhile(_ArchiveOnDisk):
    """The listing a viewer holds goes stale at midnight.

    ``odoo.log.<yesterday>`` is plain until the next rotation
    compresses it into ``.gz``; an operator who opened the viewer
    before midnight and clicks that day after it would otherwise get
    an empty day with nothing saying why. The plain name therefore
    falls back to its compressed twin — and the fallback keeps the
    same refusal of links.
    """

    def test_reading_a_plain_name_falls_back_to_its_gz(self):
        (self.logs / PLAIN_DAY).unlink()
        self._write_gz(PLAIN_DAY + ".gz", YESTERDAY)
        lines = self._lines(odoo_log_read_command(self.root, PLAIN_DAY, 100))
        self.assertEqual(lines, YESTERDAY.splitlines())

    def test_the_fallback_still_refuses_a_link(self):
        (self.logs / PLAIN_DAY).unlink()
        (self.logs / (PLAIN_DAY + ".gz")).symlink_to(self.canary)
        out = self._run(odoo_log_read_command(self.root, PLAIN_DAY, 100))
        self.assertEqual(out, b"")

    def test_downloading_a_plain_name_falls_back_to_its_gz(self):
        (self.logs / PLAIN_DAY).unlink()
        self._write_gz(PLAIN_DAY + ".gz", YESTERDAY)
        out = self._run(log_archive_download_command(self.root, PLAIN_DAY))
        self.assertEqual(gzip.decompress(out).decode(), YESTERDAY)

    def test_the_download_fallback_still_refuses_a_link(self):
        (self.logs / PLAIN_DAY).unlink()
        (self.logs / (PLAIN_DAY + ".gz")).symlink_to(self.canary)
        out = self._run(log_archive_download_command(self.root, PLAIN_DAY))
        self.assertEqual(out, b"")


class TestSweepOnDisk(_ArchiveOnDisk):

    def _sweep(self, term, **kw):
        """Run the cross-day sweep and return ``({name: count}, complete)``."""
        lines = self._lines(log_archive_search_command(self.root, term, **kw))
        complete = "IC_DONE" in lines
        hits = {}
        for line in lines:
            if line == "IC_DONE":
                continue
            name, _sep, count = line.partition("|")
            hits[name] = int(count)
        return hits, complete

    def test_the_sweep_counts_matches_per_day_and_marks_completion(self):
        hits, complete = self._sweep("needle")
        self.assertEqual(hits, {PLAIN_DAY: 1, GZ_DAY: 2})
        self.assertTrue(complete)

    def test_the_sweep_skips_the_planted_link(self):
        """The canary mentions the term; following the link would count it."""
        hits, _complete = self._sweep("needle")
        self.assertNotIn(LINKED_DAY, hits)

    def test_a_term_nobody_logged_finds_nothing_but_completes(self):
        hits, complete = self._sweep("no such thing")
        self.assertEqual(hits, {})
        self.assertTrue(complete)

    def test_the_term_is_a_fixed_string(self):
        hits, _complete = self._sweep("[a-z]+")
        self.assertEqual(hits, {})

    def test_the_file_bound_keeps_the_newest_files(self):
        """``max_files=1`` sweeps only the newest file — the live one."""
        hits, complete = self._sweep("live", max_files=1)
        self.assertEqual(hits, {"odoo.log": 3})
        self.assertTrue(complete)

    def test_a_planted_link_does_not_use_up_the_file_budget(self):
        """The link is the newest entry in ``logs/`` (it was just made).

        Skipping it is not enough: if it still counted towards
        ``max_files``, sixty planted links would leave the sweep with
        nothing real to read and an honest "no matches" to report.
        """
        # Three slots for three real files; the link must not be one.
        hits, _complete = self._sweep("needle", max_files=3)
        self.assertEqual(hits, {PLAIN_DAY: 1, GZ_DAY: 2})

    def test_junk_named_like_a_log_does_not_use_up_the_file_budget(self):
        """Only logrotate's own shapes are candidates."""
        self._write("odoo.log.evil", "needle " * 3)
        self._write("odoo.log.1", "needle\n")
        hits, _complete = self._sweep("needle", max_files=3)
        self.assertEqual(hits, {PLAIN_DAY: 1, GZ_DAY: 2})


class TestDownloadOnDisk(_ArchiveOnDisk):

    def test_downloading_a_plain_day_gzips_it(self):
        out = self._run(log_archive_download_command(self.root, PLAIN_DAY))
        self.assertEqual(gzip.decompress(out).decode(), YESTERDAY)

    def test_downloading_a_compressed_day_passes_it_through(self):
        out = self._run(log_archive_download_command(self.root, GZ_DAY))
        self.assertEqual(out, (self.logs / GZ_DAY).read_bytes())

    def test_downloading_the_planted_link_returns_nothing(self):
        out = self._run(log_archive_download_command(self.root, LINKED_DAY))
        self.assertEqual(out, b"")

    def test_the_download_is_bounded(self):
        out = self._run(
            log_archive_download_command(self.root, GZ_DAY, max_bytes=10),
        )
        self.assertEqual(len(out), 10)
