"""Tier 2 — terminal-job retention (the fourth growth leak).

cloud.job rows feed the instance timeline, so the purge keeps a long
window (settings default 180 days) and must never touch active jobs.
The tests pin the window at 5000 days so rows carried by the
pre-production snapshot stay out of scope and only the fixtures (aged
past the window by raw SQL) are eligible.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestJobPurge(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "purge-host",
                "ip_address": "10.0.0.63",
                "user": "ubuntu",
                "wildcard_domain": "purge.example.com",
            }
        )
        self.Job = self.env["cloud.job"]
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.settings.job_retention_days = 5000

    def _job(self, state, days_old):
        """Create a probe job and age it via raw SQL (ORM protects both
        ``state`` and ``create_date`` from direct writes)."""
        job = self.Job.create(
            {
                "host_id": self.host.id,
                "job_type_id": self.env.ref("incubacloud.host_probe").id,
                "name": "purge fixture",
            }
        )
        job.flush_recordset()
        self.env.cr.execute(
            "UPDATE cloud_job SET state=%s, create_date=%s WHERE id = %s",
            (
                state,
                fields.Datetime.now() - timedelta(days=days_old),
                job.id,
            ),
        )
        self.Job.invalidate_model(["state", "create_date"])
        return job

    def test_old_terminal_job_is_purged_with_its_chunks(self):
        job = self._job("done", days_old=6000)
        chunk = self.env["cloud.job.log.chunk"].create(
            {"job_id": job.id, "source": "system", "content": "x"}
        )
        self.Job._cron_purge_old()
        self.assertFalse(job.exists())
        self.assertFalse(chunk.exists())

    def test_active_job_survives_any_age(self):
        job = self._job("started", days_old=6000)
        self.Job._cron_purge_old()
        self.assertTrue(job.exists())

    def test_recent_terminal_job_survives(self):
        job = self._job("failed", days_old=10)
        self.Job._cron_purge_old()
        self.assertTrue(job.exists())

    def test_zero_disables_the_purge(self):
        job = self._job("done", days_old=6000)
        self.settings.job_retention_days = 0
        self.assertEqual(self.Job._cron_purge_old(), 0)
        self.assertTrue(job.exists())

    def test_alert_survives_with_a_nulled_job_fk(self):
        job = self._job("failed", days_old=6000)
        alert = self.env["cloud.alert"].sudo().create(
            {"code": "purge_fixture", "message": "m", "job_id": job.id}
        )
        self.Job._cron_purge_old()
        self.assertTrue(alert.exists())
        self.assertFalse(alert.job_id)
