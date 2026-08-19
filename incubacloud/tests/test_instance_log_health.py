"""The probe reads errors from the archive file, and watches the archive.

Two changes, one cause. Odoo now logs to ``logs/odoo.log`` on the host,
so the probe's ERROR scrape has to read that file — reading the
container's stdout would go blind the moment the switch lands, and the
context of an error would still be lost on every rebuild.

And because the file is the only copy, its two silent failure modes get
a watchdog of their own: Odoo falling back to stdout (the mount is not
writable) and logrotate never running (the file grows without bound).
Both look perfectly healthy from every other angle.
"""
import asyncio
import gzip
import shutil
import subprocess
import tempfile
from pathlib import Path

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.instance_health_executor import (
    _LOG_MAX_BYTES,
    InstanceHealthExecutor,
)


class TestProbeReadsTheArchiveFile(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create(
            {"name": "LogHealth Proj"},
        )
        self.host = self.env["cloud.host"].create({
            "name": "loghealth-host",
            "ip_address": "192.0.2.65",
            "user": "ubuntu",
            "wildcard_domain": "loghealth.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "loghealthinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _commands(self):
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        return {item[0]: item[1] for item in executor.get_commands()}

    def test_error_scrape_reads_the_log_file(self):
        cmd = self._commands()["error_lines"]
        self.assertIn("logs/odoo.log", cmd)

    def test_error_scrape_still_falls_back_to_container_output(self):
        """Until an instance is rebuilt there is no file to read."""
        cmd = self._commands()["error_lines"]
        self.assertIn("docker compose logs", cmd)

    def test_error_scrape_reads_the_newest_archive_too(self):
        """Rotation happens at midnight; the window straddles it once a day."""
        cmd = self._commands()["error_lines"]
        self.assertIn("odoo\\.log\\.", cmd)
        self.assertIn("zcat -f", cmd)

    def test_error_scrape_keeps_the_error_grep_and_its_context(self):
        cmd = self._commands()["error_lines"]
        self.assertIn("ERROR|CRITICAL", cmd)
        self.assertIn("-A ", cmd)

    def test_error_scrape_filters_by_timestamp(self):
        """``docker logs --since`` is gone; the file has no such switch."""
        self.instance.write({"last_health_check": "2026-08-18 07:00:00"})
        cmd = self._commands()["error_lines"]
        self.assertIn("2026-08-18", cmd)

    def test_the_probe_reports_the_state_of_the_log_file(self):
        self.assertIn("log_health", self._commands())

    def test_error_scrape_picks_the_newest_archive_among_regular_files(self):
        """``logs/`` belongs to the container's uid; a link planted there
        as the newest ``odoo.log.<date>`` must not be what the probe
        decompresses on the host."""
        cmd = self._commands()["error_lines"]
        self.assertIn("-type f", cmd)
        self.assertNotIn("ls -1t", cmd)

    def test_error_scrape_does_not_follow_a_link_as_the_live_file(self):
        cmd = self._commands()["error_lines"]
        self.assertIn("! -L logs/odoo.log", cmd)

    def test_log_health_does_not_size_a_link(self):
        cmd = self._commands()["log_health"]
        self.assertIn("! -L logs/odoo.log", cmd)


class TestProbeReadsRegularFilesOnly(TransactionCase):
    """The error scrape, run against real files on disk.

    Same hole as the viewer's, same guard: the probe runs on the host
    as the SSH user and ``logs/`` is writable from inside the container,
    so a symlink planted as the newest archive (or in place of the live
    file) would make the probe read a host file — and carry whatever
    looked like an Odoo ERROR line into an alert.
    """

    ERROR_LIVE = (
        "2026-08-19 09:00:00,000 1 ERROR db odoo.sql_db: live boom\n"
        "Traceback (most recent call last):\n"
        "  File \"x.py\", line 1\n"
    )
    ERROR_ARCHIVED = (
        "2026-08-18 23:59:00,000 1 ERROR db odoo.sql_db: archived boom\n"
    )
    CANARY = (
        "2026-08-19 08:00:00,000 1 ERROR db odoo.sql_db: HOST-SECRET boom\n"
    )

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create(
            {"name": "LogProbe Proj"},
        )
        self.host = self.env["cloud.host"].create({
            "name": "logprobe-host",
            "ip_address": "192.0.2.67",
            "user": "ubuntu",
            "wildcard_domain": "logprobe.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "logprobeinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )
        self.root = tempfile.mkdtemp(prefix="ic-probe-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.logs = Path(self.root) / "logs"
        self.logs.mkdir()
        self.canary = Path(self.root) / "CANARY"
        self.canary.write_text(self.CANARY, encoding="utf-8")

    def _executor(self):
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        return executor

    def _scrape(self):
        """Run the error scrape against the temporary directory."""
        cmd = self._executor()._error_lines_command(
            self.root, "10m", "2000-01-01 00:00:00",
        )
        proc = subprocess.run(
            ["sh", "-c", cmd], capture_output=True, timeout=60, check=False,
        )
        return proc.stdout.decode("utf-8", "replace")

    def test_the_scrape_reads_the_live_file_and_the_newest_archive(self):
        (self.logs / "odoo.log").write_text(self.ERROR_LIVE, encoding="utf-8")
        with gzip.open(
            self.logs / "odoo.log.2026-08-18.gz", "wt", encoding="utf-8",
        ) as fh:
            fh.write(self.ERROR_ARCHIVED)
        out = self._scrape()
        self.assertIn("live boom", out)
        self.assertIn("archived boom", out)
        self.assertIn("Traceback", out, "the context below a header is kept")

    def test_a_link_planted_as_the_newest_archive_is_not_read(self):
        (self.logs / "odoo.log").write_text(self.ERROR_LIVE, encoding="utf-8")
        (self.logs / "odoo.log.2026-08-19").symlink_to(self.canary)
        out = self._scrape()
        self.assertIn("live boom", out)
        self.assertNotIn("HOST-SECRET", out)

    def test_a_link_in_place_of_the_live_file_is_not_read(self):
        (self.logs / "odoo.log").symlink_to(self.canary)
        out = self._scrape()
        self.assertNotIn("HOST-SECRET", out)


class TestLogHealthAlerts(TransactionCase):

    def setUp(self):
        super().setUp()
        # The probe writes alerts on a cursor of its own; test mode lets
        # that cursor see the records created here.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create(
            {"name": "LogAlert Proj"},
        )
        self.host = self.env["cloud.host"].create({
            "name": "logalert-host",
            "ip_address": "192.0.2.66",
            "user": "ubuntu",
            "wildcard_domain": "logalert.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "logalertinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _probe(self, log_health):
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        states = "\n".join(
            f"{svc}\trunning" for svc in self.instance.expected_services()
        )
        results = {
            "container_state": {"stdout": states},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": "exit:0"},
            "error_lines": {"stdout": ""},
            "log_health": {"stdout": log_health},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))
        return executor

    def _alert(self):
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_logs_unhealthy"),
            ("state", "=", "active"),
        ])

    def test_a_healthy_archive_raises_nothing(self):
        self._probe("dir:1\nsize:4096\nstdout:0")
        self.assertFalse(self._alert())

    def test_an_instance_without_the_mount_yet_raises_nothing(self):
        """Not rebuilt since the feature shipped: nothing to grade."""
        self._probe("dir:0\nsize:0\nstdout:0")
        self.assertFalse(self._alert())

    def test_odoo_logging_to_stdout_is_an_alert(self):
        """The mount exists but Odoo is not writing to it."""
        self._probe("dir:1\nsize:0\nstdout:42")
        alert = self._alert()
        self.assertTrue(alert)
        self.assertEqual(alert.payload.get("reason"), "fallback")

    def test_one_stray_line_is_not_an_alert(self):
        """Startup noise reaches stdout; only a real stream counts."""
        self._probe("dir:1\nsize:4096\nstdout:1")
        self.assertFalse(self._alert())

    def test_a_log_that_never_rotates_is_an_alert(self):
        self._probe(f"dir:1\nsize:{_LOG_MAX_BYTES + 1}\nstdout:0")
        alert = self._alert()
        self.assertTrue(alert)
        self.assertEqual(alert.payload.get("reason"), "rotation_stalled")

    def test_recovery_dismisses_the_alert(self):
        self._probe("dir:1\nsize:0\nstdout:42")
        self.assertTrue(self._alert())
        self._probe("dir:1\nsize:4096\nstdout:0")
        self.assertFalse(self._alert())

    def test_a_probe_without_the_reading_grades_nothing(self):
        """Old executors in flight (or a skipped cycle) must not alert."""
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        states = "\n".join(
            f"{svc}\trunning" for svc in self.instance.expected_services()
        )
        results = {
            "container_state": {"stdout": states},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": "exit:0"},
            "error_lines": {"stdout": ""},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))
        self.assertFalse(self._alert())
