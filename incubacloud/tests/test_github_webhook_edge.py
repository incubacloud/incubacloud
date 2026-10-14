"""Noticing when GitHub webhook deliveries stop arriving.

An edge allowlist in front of the endpoint fails by dropping deliveries
silently — a range GitHub adds and nobody mirrors stops pushes with no
error anywhere. Silence alone cannot tell that from a quiet repository,
so the check asks GitHub what it tried to deliver and compares it with
what was recorded here: attempts that never landed are the fault, a
window with no attempts is a quiet week.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase

from ..github.client import GitHubAPIError, GitHubAppClient
from ..models.cloud_github_event import GITHUB_WEBHOOK_SILENT_CODE


def _attempt(hours_ago, guid, status="OK", status_code=200):
    """Render one entry of GitHub's delivery log, *hours_ago* old."""
    when = fields.Datetime.now() - timedelta(hours=hours_ago)
    return {
        "guid": guid,
        "delivered_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "status_code": status_code,
        "event": "push",
        "redelivery": False,
    }



class TestWebhookSilenceAlert(TransactionCase):
    """Attempts GitHub made against deliveries recorded here."""

    def setUp(self):
        """Start from a platform with an App configured and no events."""
        super().setUp()
        self.Event = self.env["cloud.github.event"].sudo()
        self.Alert = self.env["cloud.alert"].sudo()
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.settings.github_webhook_silence_hours = 48
        self.Event.search([]).unlink()
        self.env["cloud.github.app"].sudo().search([]).unlink()
        self.env["cloud.github.app"].sudo().create({
            "app_id": "12345",
            "private_key": "test-key",
        })
        # The database under test may carry a standing alert from before.
        self.Alert.resolve_alert(GITHUB_WEBHOOK_SILENT_CODE)

    def _active(self):
        """Return the standing silence alert, if any."""
        return self.Alert.search(
            self.Alert._dedup_domain(GITHUB_WEBHOOK_SILENT_CODE), limit=1,
        )

    def _check(self, deliveries=None, error=None):
        """Run the cron with GitHub answering *deliveries*, or *error*."""
        kwargs = {"side_effect": error} if error else {"return_value": deliveries or []}
        with patch.object(
            GitHubAppClient, "list_hook_deliveries", autospec=True, **kwargs,
        ) as listed:
            result = self.Event._cron_check_delivery_silence()
        self.listed = listed
        return result

    def _backdate(self, event, hours):
        """Move *event* into the past, bypassing the ORM's create_date."""
        when = fields.Datetime.now() - timedelta(hours=hours)
        self.env.cr.execute(
            "UPDATE cloud_github_event SET create_date = %s WHERE id = %s",
            (when, event.id),
        )
        event.invalidate_recordset(["create_date"])

    def test_silence_with_nothing_attempted_is_a_quiet_week(self):
        """No push happened; nothing is wrong."""
        self._check(deliveries=[])
        self.assertFalse(self._active())

    def test_attempts_that_never_arrived_raise_the_alert(self):
        """The allowlist case: GitHub tried, the edge answered 403."""
        self._check(deliveries=[
            _attempt(2, "d-1", "Invalid HTTP Response: 403", 403),
            _attempt(5, "d-2", "Invalid HTTP Response: 403", 403),
            _attempt(9, "d-3", "Invalid HTTP Response: 403", 403),
        ])
        alert = self._active()
        self.assertTrue(alert)
        self.assertIn("3 of 3", alert.message)
        self.assertIn("403", alert.message)

    def test_the_alert_repeats_what_github_saw(self):
        """Delivered 200 and still no row: lost past the edge, said so."""
        self._check(deliveries=[_attempt(3, "d-1", "OK", 200)])
        alert = self._active()
        self.assertTrue(alert)
        self.assertIn("OK", alert.message)

    def test_a_recorded_delivery_matches_its_attempt(self):
        """Traffic that arrived is proof the edge lets GitHub through,
        and it clears a standing alert."""
        self._check(deliveries=[_attempt(3, "d-0", "Invalid HTTP Response: 403", 403)])
        self.assertTrue(self._active())
        self.Event.create({"event_type": "push", "delivery_id": "d-1"})
        self._check(deliveries=[_attempt(1, "d-1"), _attempt(3, "d-0")])
        self.assertFalse(self._active(), "d-0 is older than the recorded d-1")

    def test_a_failed_processing_still_counts_as_received(self):
        """A processing error has its own alert; the edge is clearly fine."""
        self.Event.create({
            "event_type": "push", "delivery_id": "d-2",
            "processed": False, "error": "boom",
        })
        self._check(deliveries=[_attempt(1, "d-2")])
        self.assertFalse(self._active())

    def test_a_drop_after_the_last_arrival_is_noticed(self):
        """Some arrived, then the edge changed: the newer ones are lost."""
        landed = self.Event.create({"event_type": "push", "delivery_id": "d-1"})
        self._backdate(landed, hours=6)
        self._check(deliveries=[
            _attempt(1, "d-3", "Invalid HTTP Response: 403", 403),
            _attempt(2, "d-2", "Invalid HTTP Response: 403", 403),
            _attempt(6, "d-1"),
        ])
        alert = self._active()
        self.assertTrue(alert)
        self.assertIn("2 of 3", alert.message)

    def test_a_fresh_attempt_is_given_time_to_land(self):
        """The controller may still be writing a delivery made seconds ago."""
        self._check(deliveries=[_attempt(0.05, "d-1")])
        self.assertFalse(self._active())

    def test_an_attempt_outside_the_window_is_not_counted(self):
        self._check(deliveries=[_attempt(72, "d-1", "Invalid HTTP Response: 403", 403)])
        self.assertFalse(self._active())

    def test_an_old_delivery_does_not_hold_the_alert_off(self):
        """The window is what matters, not that traffic once existed."""
        event = self.Event.create({"event_type": "push", "delivery_id": "d-3"})
        self._backdate(event, hours=72)
        self._check(deliveries=[_attempt(4, "d-4", "Invalid HTTP Response: 403", 403)])
        self.assertTrue(self._active())

    def test_github_unreadable_decides_nothing(self):
        """Blind this hour: neither raise nor clear."""
        self._check(error=GitHubAPIError(503, "down"))
        self.assertFalse(self._active())
        self._check(deliveries=[_attempt(3, "d-1", "Invalid HTTP Response: 403", 403)])
        self.assertTrue(self._active())
        self._check(error=GitHubAPIError(503, "down"))
        self.assertTrue(self._active(), "an unreadable log cleared a real alert")

    def test_recent_traffic_does_not_ask_github(self):
        """A delivery recorded in the last hour is proof enough; the log
        is read only when the panel has been quiet for an hour."""
        self.Event.create({"event_type": "push", "delivery_id": "d-1"})
        self._check(deliveries=[])
        self.listed.assert_not_called()
        self.assertFalse(self._active())

    def test_no_app_configured_never_alerts(self):
        """An install that never wired GitHub up is not broken."""
        self.env["cloud.github.app"].sudo().search([]).unlink()
        self._check(deliveries=[_attempt(3, "d-1", "Invalid HTTP Response: 403", 403)])
        self.listed.assert_not_called()
        self.assertFalse(self._active())

    def test_zero_hours_disables_the_check_and_clears_it(self):
        """Explicit opt-out, and turning it off resolves what it raised."""
        self._check(deliveries=[_attempt(3, "d-1", "Invalid HTTP Response: 403", 403)])
        self.assertTrue(self._active())
        self.settings.github_webhook_silence_hours = 0
        self._check(deliveries=[])
        self.listed.assert_not_called()
        self.assertFalse(self._active())
