"""How an alert left the active list, and who was told.

A failed job's alert is dismissed when a later run of the same job on
the same target succeeds. That dismissal was silent: the failure had
reached email and Telegram, and the panel, opened later, showed only a
dismissed row, the same as one a user had silenced (two warm rebuilds
on 2026-10-08). Now the alert records how it closed, when, and which
job resolved it, and the resolution is announced on the channels that
announced the failure.
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAlertResolution(TransactionCase):

    def setUp(self):
        """A host, and a user subscribed to every alert by email and Telegram."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "resolution-host",
            "ip_address": "10.0.0.61",
            "user": "ubuntu",
            "wildcard_domain": "resolution.example.com",
        })
        # Neutralise admin's channels so nothing but the user below is
        # notified.
        self.env["res.users"].sudo().browse(2).write({
            "cloud_telegram_bot_token": "",
            "cloud_telegram_chat_id": "",
            "cloud_webhook_url": "",
        })
        self.env["res.users"].create({
            "name": "resolution-watcher",
            "login": "resolution-watcher",
            "email": "resolution-watcher@example.com",
            "group_ids": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("incubacloud.group_cloud_project_manager").id),
            ],
            "cloud_notification_level": "all",
            "cloud_telegram_bot_token": "test-bot-token",
            "cloud_telegram_chat_id": "123456789",
        })

    def _job(self, code, uuid):
        """A cloud.job on the host and its queue.job, still pending."""
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        if not job_type:
            job_type = self.env["cloud.job.type"].create(
                {"name": code, "code": code, "apply_to": "host"},
            )
        cjob = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": job_type.id,
            "name": f"Job {code}",
            "queue_job_uuid": uuid,
        })
        qjob = self.env["queue.job"].sudo().with_context(
            test_external_notify=True,
        ).create({
            "uuid": uuid,
            "name": f"qj-{code}",
            "state": "pending",
            "method_name": "noop",
            "model_name": "cloud.job",
        })
        return cjob, qjob

    def _resolved_mails(self):
        """The resolution emails the watcher received."""
        return self.env["mail.mail"].sudo().search([
            ("subject", "like", "[IncubaCloud] Resolved:%"),
            ("email_to", "=", "resolution-watcher@example.com"),
        ])

    @staticmethod
    def _telegram_texts(urlopen):
        """The texts posted to Telegram through the mocked ``safe_urlopen``."""
        return [
            json.loads(call.args[0].data)["text"] for call in urlopen.call_args_list
        ]

    @patch("odoo.addons.incubacloud.models.cloud_alert.safe_urlopen")
    def test_a_later_success_resolves_the_failure_and_says_so(self, urlopen):
        failed, qfailed = self._job("rebuild_instance", "uuid-resolution-a")
        qfailed.write({"state": "failed", "exc_message": "Connection lost"})
        alert = self.env["cloud.alert"].search([
            ("job_id", "=", failed.id), ("state", "=", "active"),
        ])
        self.assertEqual(len(alert), 1)
        self.assertFalse(alert.resolution)

        succeeded, qsucceeded = self._job("rebuild_instance", "uuid-resolution-b")
        qsucceeded.write({"state": "done"})

        alert.invalidate_recordset()
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(alert.resolution, "auto")
        self.assertEqual(alert.resolved_by_job_id, succeeded)
        self.assertTrue(alert.resolved_at)
        mails = self._resolved_mails()
        self.assertEqual(len(mails), 1)
        self.assertIn(succeeded.name, mails.body_html)
        resolved = [t for t in self._telegram_texts(urlopen) if "*Resolved:*" in t]
        self.assertEqual(len(resolved), 1)
        self.assertIn(f"Resolved by: {succeeded.name}", resolved[0])

    def test_a_dismissal_by_hand_is_told_apart(self):
        alert = self.env["cloud.alert"].sudo().create({
            "code": "disk_warning",
            "message": "Disk at 85%",
            "host_id": self.host.id,
        })
        self.env["cloud.alert"].action_dismiss_all()
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(alert.resolution, "manual")
        self.assertTrue(alert.resolved_at)
        self.assertFalse(self._resolved_mails())

    def test_a_cleared_condition_resolves_automatically(self):
        Alert = self.env["cloud.alert"]
        alert = Alert.raise_alert("disk_warning", "Disk at 85%", host=self.host)
        Alert.resolve_alert("disk_warning", host=self.host)
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(alert.resolution, "auto")
        self.assertFalse(alert.resolved_by_job_id)
        self.assertEqual(len(self._resolved_mails()), 1)

    def test_a_closed_alert_keeps_when_it_closed(self):
        alert = self.env["cloud.alert"].sudo().create({
            "code": "disk_warning",
            "message": "Disk at 85%",
            "host_id": self.host.id,
        })
        alert.write({"state": "dismissed"})
        closed_at = alert.resolved_at
        self.assertEqual(alert.resolution, "auto")
        alert.write({"state": "dismissed", "resolution": "manual"})
        self.assertEqual(alert.resolved_at, closed_at)
        self.assertEqual(alert.resolution, "manual")
