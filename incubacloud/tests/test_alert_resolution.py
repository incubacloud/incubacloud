"""How an alert left the active list, and who was told.

A failed job's alert is dismissed when a later run of the same job on
the same target succeeds. That dismissal was silent: the failure had
reached email and Telegram, and the panel, opened later, showed only a
dismissed row, the same as one a user had silenced (two rebuilds on
2026-10-08). Now the alert records how it closed, when, and which
job resolved it, and the resolution is announced on the channels that
announced the failure.
"""
import json
from unittest.mock import patch

from psycopg2.errors import SerializationFailure

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
            # The panel's host sits between the tag and the verdict.
            ("subject", "like", "[IncubaCloud]%Resolved:%"),
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
        # The dismissal runs on a cursor of its own after the commit: in
        # test mode that cursor shares this transaction.
        self.registry_enter_test_mode()
        failed, qfailed = self._job("rebuild_instance", "uuid-resolution-a")
        qfailed.write({"state": "failed", "exc_message": "Connection lost"})
        alert = self.env["cloud.alert"].search([
            ("job_id", "=", failed.id), ("state", "=", "active"),
        ])
        self.assertEqual(len(alert), 1)
        self.assertFalse(alert.resolution)

        succeeded, qsucceeded = self._job("rebuild_instance", "uuid-resolution-b")
        qsucceeded.write({"state": "done"})
        # Nothing is dismissed inside the job's transaction: it waits
        # for the commit (2026-11-02, a job run twice over an alert
        # dismissed by hand while it ran).
        self.assertEqual(alert.state, "active")
        self.env.flush_all()
        self.env.cr.postcommit.run()

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
        # Markdown: the name's underscore is escaped (``telegram_markdown``).
        self.assertIn("Resolved by: Job rebuild\\_instance", resolved[0])

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


@tagged("post_install", "-at_install")
class TestAResolutionThatLosesTheRow(TransactionCase):
    """A cron resolving an alert someone dismissed while it ran.

    The cron's snapshot still shows the alert active; PostgreSQL refuses
    its write because the row changed since. That lost the cron's whole
    pass (inventory H7). The write now runs in a savepoint: an alert
    another writer already closed is left as it is, and one that is
    still active raises as before.
    """

    def setUp(self):
        super().setUp()
        # The alert is read again on a cursor of its own: in test mode
        # it rides on this transaction.
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "lost-row-host",
            "ip_address": "10.0.0.62",
            "user": "ubuntu",
            "wildcard_domain": "lost-row.example.com",
        })
        self.Alert = self.env["cloud.alert"]
        self.alert = self.Alert.raise_alert(
            "disk_warning", "Disk at 85%", host=self.host,
        )
        self.assertEqual(self.alert.state, "active")
        self.env.flush_all()

    def _resolve_losing_the_row(self):
        """Resolve the alert as a snapshot that still sees it active.

        ``search`` answers what that snapshot would, and ``write`` is
        refused the way PostgreSQL refuses the second writer of a row.
        """
        Model = type(self.Alert)
        with patch.object(
            Model, "search", autospec=True, return_value=self.alert,
        ), patch.object(
            Model, "write", autospec=True,
            side_effect=SerializationFailure("could not serialize access"),
        ), patch.object(
            Model, "_notify_alert_resolved", autospec=True,
        ) as notify:
            try:
                return self.Alert.resolve_alert("disk_warning", host=self.host)
            finally:
                notify.assert_not_called()

    def test_an_alert_dismissed_meanwhile_is_left_alone(self):
        self.env.cr.execute(
            "UPDATE cloud_alert SET state = 'dismissed', resolution = 'manual'"
            " WHERE id = %s", (self.alert.id,),
        )
        resolved = self._resolve_losing_the_row()
        self.assertFalse(resolved)
        # The transaction goes on: the rest of the cron's pass stands.
        self.alert.invalidate_recordset()
        self.assertEqual(self.alert.state, "dismissed")
        self.assertEqual(self.alert.resolution, "manual")

    def test_an_alert_still_active_raises_as_before(self):
        with self.assertRaises(SerializationFailure):
            self._resolve_losing_the_row()
        self.alert.invalidate_recordset()
        self.assertEqual(self.alert.state, "active")

    def test_raising_an_alert_is_not_made_tolerant(self):
        """``raise_alert`` stays as it was: only the closing is forgiven."""
        Model = type(self.Alert)
        with patch.object(
            Model, "write", autospec=True,
            side_effect=SerializationFailure("could not serialize access"),
        ), self.assertRaises(SerializationFailure):
            self.Alert.raise_alert("disk_warning", "Disk at 90%", host=self.host)
