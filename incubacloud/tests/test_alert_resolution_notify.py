"""Tier 2 — external channels see alert *resolutions*, not only raises.

Before this, ``resolve_alert`` only flipped state: a webhook/Telegram
consumer that opened an incident on the ``alert`` event never learned it
closed, so every incident looked permanently open from on-call.
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAlertResolutionNotify(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "resolve-notify-host",
                "ip_address": "10.0.0.61",
                "user": "ubuntu",
                "wildcard_domain": "resolve.example.com",
            }
        )
        # Neutralise admin so only our fixture user receives sends.
        self.env["res.users"].sudo().browse(2).write(
            {
                "cloud_telegram_bot_token": "",
                "cloud_telegram_chat_id": "",
                "cloud_webhook_url": "",
            }
        )
        self.user = self.env["res.users"].create(
            {
                "name": "hook-user",
                "login": "hook-user@example.com",
                "email": "hook-user@example.com",
                "group_ids": [
                    (4, self.env.ref("base.group_user").id),
                    (
                        4,
                        self.env.ref(
                            "incubacloud.group_cloud_project_manager"
                        ).id,
                    ),
                ],
                "cloud_notification_level": "all",
                "cloud_webhook_url": "https://hooks.example.com/x",
            }
        )
        self.Alert = self.env["cloud.alert"].with_context(
            test_external_notify=True,
        )

    @staticmethod
    def _events(mock_open):
        return [
            json.loads(call.args[0].data.decode())["event"]
            for call in mock_open.call_args_list
        ]

    @patch("odoo.addons.incubacloud.models.cloud_alert.urllib.request.urlopen")
    def test_resolution_reaches_the_webhook(self, mock_open):
        self.Alert.raise_alert(
            "disk_critical", "disk almost full", level="critical",
            host=self.host,
        )
        self.Alert.resolve_alert("disk_critical", host=self.host)
        events = self._events(mock_open)
        self.assertIn("alert", events)
        self.assertIn("alert_resolved", events)

    @patch("odoo.addons.incubacloud.models.cloud_alert.urllib.request.urlopen")
    def test_manual_dismiss_stays_silent(self, mock_open):
        """An operator dismissing by hand is already looking at the
        panel; only automatic resolution announces closure."""
        alert = self.Alert.raise_alert(
            "disk_critical", "disk almost full", level="critical",
            host=self.host,
        )
        mock_open.reset_mock()
        alert.write({"state": "dismissed"})
        self.assertEqual(self._events(mock_open), [])

    @patch("odoo.addons.incubacloud.models.cloud_alert.urllib.request.urlopen")
    def test_resolving_nothing_sends_nothing(self, mock_open):
        self.Alert.resolve_alert("disk_critical", host=self.host)
        self.assertEqual(self._events(mock_open), [])
