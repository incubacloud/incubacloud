"""Tier 2 — long-running-job watch (duration ceiling alert, P12.5).

The runner kills jobs at ``limit_time_real`` (60 min); the watch warns
at 45 so the operator tunes the limit from evidence, not guesses.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestJobDurationWatch(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "duration-host",
                "ip_address": "10.0.0.62",
                "user": "ubuntu",
                "wildcard_domain": "duration.example.com",
            }
        )
        self.Job = self.env["cloud.job"]
        self.Alert = self.env["cloud.alert"].sudo()
        # The watch scans every started job; neutralise rows carried by
        # the pre-production snapshot (rolled back with the test).
        self.env.cr.execute(
            "UPDATE cloud_job SET state='done' WHERE state='started'"
        )
        self.Job.invalidate_model(["state"])

    def _job(self, minutes_old):
        job = self.Job.create(
            {
                "host_id": self.host.id,
                "job_type_id": self.env.ref("incubacloud.host_probe").id,
                "name": "duration fixture",
            }
        )
        job.flush_recordset()
        self.env.cr.execute(
            "UPDATE cloud_job SET state='started', create_date=%s "
            "WHERE id = %s",
            (
                fields.Datetime.now() - timedelta(minutes=minutes_old),
                job.id,
            ),
        )
        self.Job.invalidate_model(["state", "create_date"])
        return job

    def _active_alert(self):
        return self.Alert.search(
            [
                ("code", "=", self.Job._LONG_RUNNING_ALERT_CODE),
                ("state", "=", "active"),
                ("host_id", "=", self.host.id),
            ]
        )

    def test_long_runner_raises_a_warning(self):
        self._job(minutes_old=50)
        self.Job._cron_watch_long_running()
        alert = self._active_alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")

    def test_fresh_job_stays_silent(self):
        self._job(minutes_old=5)
        self.Job._cron_watch_long_running()
        self.assertFalse(self._active_alert())

    def test_alert_resolves_when_the_job_ends(self):
        job = self._job(minutes_old=50)
        self.Job._cron_watch_long_running()
        self.assertTrue(self._active_alert())
        job.flush_recordset()
        self.env.cr.execute(
            "UPDATE cloud_job SET state='done' WHERE id = %s", (job.id,)
        )
        self.Job.invalidate_model(["state"])
        self.Job._cron_watch_long_running()
        self.assertFalse(self._active_alert())
