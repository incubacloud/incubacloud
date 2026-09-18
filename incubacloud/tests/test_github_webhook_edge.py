"""Noticing when GitHub webhook deliveries stop.

An edge allowlist in front of the endpoint fails by dropping deliveries
silently — a range GitHub adds and nobody mirrors stops pushes with no
error anywhere. The alert here is the only thing that would notice, so
it lives with the event model regardless of who publishes the allowlist.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase

from ..models.cloud_github_event import GITHUB_WEBHOOK_SILENT_CODE


class TestWebhookSilenceAlert(TransactionCase):
    """Deliveries stopping is invisible unless something watches for it."""

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

    def _active(self):
        """Return the standing silence alert, if any."""
        return self.Alert.search(
            self.Alert._dedup_domain(GITHUB_WEBHOOK_SILENT_CODE), limit=1,
        )

    def _backdate(self, event, hours):
        """Move *event* into the past, bypassing the ORM's create_date."""
        when = fields.Datetime.now() - timedelta(hours=hours)
        self.env.cr.execute(
            "UPDATE cloud_github_event SET create_date = %s WHERE id = %s",
            (when, event.id),
        )
        event.invalidate_recordset(["create_date"])

    def test_silence_with_an_app_configured_raises_the_alert(self):
        """No delivery at all is the case the allowlist could cause."""
        self.Event._cron_check_delivery_silence()
        alert = self._active()
        self.assertTrue(alert)
        self.assertIn("never", alert.message)

    def test_a_recent_delivery_keeps_it_quiet_and_clears_a_standing_alert(self):
        """Traffic arriving is proof the edge is letting GitHub through."""
        self.Event._cron_check_delivery_silence()
        self.assertTrue(self._active())
        self.Event.create({"event_type": "push", "delivery_id": "d-1"})
        self.Event._cron_check_delivery_silence()
        self.assertFalse(self._active())

    def test_a_failed_delivery_still_counts_as_traffic(self):
        """A processing error has its own alert; the edge is clearly fine."""
        self.Event.create({
            "event_type": "push",
            "delivery_id": "d-2",
            "processed": False,
            "error": "boom",
        })
        self.Event._cron_check_delivery_silence()
        self.assertFalse(self._active())

    def test_an_old_delivery_does_not_hold_the_alert_off(self):
        """The window is what matters, not that traffic once existed."""
        event = self.Event.create({"event_type": "push", "delivery_id": "d-3"})
        self._backdate(event, hours=72)
        self.Event._cron_check_delivery_silence()
        self.assertTrue(self._active())

    def test_no_app_configured_never_alerts(self):
        """An install that never wired GitHub up is not broken."""
        self.env["cloud.github.app"].sudo().search([]).unlink()
        self.Event._cron_check_delivery_silence()
        self.assertFalse(self._active())

    def test_zero_hours_disables_the_check_and_clears_it(self):
        """Explicit opt-out, and turning it off resolves what it raised."""
        self.Event._cron_check_delivery_silence()
        self.assertTrue(self._active())
        self.settings.github_webhook_silence_hours = 0
        self.Event._cron_check_delivery_silence()
        self.assertFalse(self._active())
