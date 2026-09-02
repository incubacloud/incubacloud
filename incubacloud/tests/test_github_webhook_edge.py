"""Reading GitHub's published hook ranges, and noticing when deliveries stop.

An edge allowlist built from ``api.github.com/meta`` is the only thing
that removes the cost of verifying a forged HMAC rather than bounding it.
It also introduces a failure mode the platform did not have: a range
GitHub adds and we never mirror stops deliveries with no error anywhere.
These tests pin both halves — the document is only trusted when it fully
parses, and the silence it could cause is alarmed on.
"""
import io
import json
import urllib.error
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase

from ..github import meta
from ..github.meta import (
    GitHubMetaError,
    MAX_HOOK_RANGES,
    fetch_hook_ranges,
    normalize_hook_ranges,
)
from ..models.cloud_github_event import GITHUB_WEBHOOK_SILENT_CODE


class TestHookRangeParsing(TransactionCase):
    """A half-understood /meta document must never become an allowlist."""

    def test_published_ranges_are_normalised_in_order(self):
        """Both address families survive, in the order GitHub published."""
        self.assertEqual(
            normalize_hook_ranges({
                "hooks": ["192.30.252.0/22", "2a0a:a440::/29"],
            }),
            ["192.30.252.0/22", "2a0a:a440::/29"],
        )

    def test_a_document_without_hooks_is_refused(self):
        """Missing or empty means we know nothing, not that nobody is allowed."""
        for payload in ({}, {"hooks": []}, {"hooks": "not-a-list"}, []):
            with self.assertRaises(GitHubMetaError):
                normalize_hook_ranges(payload)

    def test_an_entry_that_is_not_a_network_is_refused(self):
        """One unparseable entry invalidates the whole document."""
        with self.assertRaises(GitHubMetaError):
            normalize_hook_ranges({"hooks": ["192.30.252.0/22", "nonsense"]})
        with self.assertRaises(GitHubMetaError):
            normalize_hook_ranges({"hooks": [1234]})

    def test_an_implausibly_long_list_is_refused(self):
        """A list far past any real answer is a malformed document."""
        oversized = [f"10.{n}.0.0/16" for n in range(MAX_HOOK_RANGES + 1)]
        with self.assertRaises(GitHubMetaError):
            normalize_hook_ranges({"hooks": oversized})

    def test_a_host_address_is_normalised_to_its_network(self):
        """``strict=False`` keeps a bare address usable as a /32."""
        self.assertEqual(
            normalize_hook_ranges({"hooks": ["1.2.3.4/32", "192.30.252.1/24"]}),
            ["1.2.3.4/32", "192.30.252.0/24"],
        )


class TestHookRangeFetch(TransactionCase):
    """The fetch refuses redirects and bounds what it will read."""

    @contextmanager
    def _response(self, body):
        """Return a context-manager response serving *body*.

        :param body: raw bytes the endpoint answers with
        """
        stream = io.BytesIO(body)

        @contextmanager
        def _opened(*_args, **_kwargs):
            yield stream

        with patch.object(meta, "safe_urlopen", _opened):
            yield

    def test_a_well_formed_document_yields_its_ranges(self):
        """The happy path goes through the no-redirect opener."""
        payload = json.dumps({
            "hooks": ["192.30.252.0/22"],
            "web": ["203.0.113.0/24"],
        }).encode()
        with self._response(payload):
            self.assertEqual(fetch_hook_ranges(), ["192.30.252.0/22"])

    def test_a_transport_failure_raises_the_module_error(self):
        """Callers only have to handle one exception type."""
        with patch.object(
            meta, "safe_urlopen", side_effect=urllib.error.URLError("down"),
        ), self.assertRaises(GitHubMetaError):
            fetch_hook_ranges()

    def test_a_non_json_body_raises_the_module_error(self):
        """A proxy interception page must not become an allowlist."""
        with self._response(b"<html>captive portal</html>"):
            with self.assertRaises(GitHubMetaError):
                fetch_hook_ranges()


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
