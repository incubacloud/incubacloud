"""The agents follow the direct path, and a host gone quiet on it is retried.

The reconciliation re-applies a host whose agents were installed with
another URL than the one it is offered now, and once a host that has a
direct check it never tried. A host installed on the direct path whose
metrics stopped for 20 minutes is re-applied, at most once an hour, only
while the central is healthy and, when other hosts write directly, one of
them is still up to date: a central outage must not re-apply the fleet.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields

from odoo.addons.incubacloud.tests._direct import make_host
from odoo.addons.incubacloud.tests.test_direct_agents import (
    OFFER,
    PUBLIC,
    _OfferCase,
)


class _ConvergenceCase(_OfferCase):
    """A prepared host whose agents are installed, and a central just read."""

    def setUp(self):
        super().setUp()
        self.host.write({
            "known_hosts_key": "hx.example.com ssh-ed25519 AAAA",
            "traefik_deployed": True,
            "metrics_agents_state": "installed",
            "metrics_agents_since": fields.Datetime.now() - timedelta(hours=2),
            "metrics_last_seen": fields.Datetime.now(),
        })
        self.settings.metrics_read_ok_at = (
            fields.Datetime.now() - timedelta(minutes=2)
        )

    def _queued(self, host=None):
        """Return how many agent installs are queued on *host* (the test's)."""
        return self.env["cloud.job"].search_count([
            ("host_id", "=", (host or self.host).id),
            ("job_type_id.code", "=", "install_observability"),
        ])


class TestTheAgentsFollowTheDirectPath(_ConvergenceCase):

    def test_a_host_installed_on_the_public_url_is_moved_when_offered(self):
        self.host.metrics_agents_write_url = PUBLIC
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 1)

    def test_an_empty_url_is_the_public_one(self):
        self.settings.metrics_central_direct_state = "failing"
        self.host.metrics_agents_write_url = False
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 0)

    def test_a_host_on_the_url_it_should_have_is_left_alone(self):
        self.host.write({
            "metrics_agents_write_url": OFFER,
            "direct_reach_ok": True,
            "direct_reach_urls": OFFER,
        })
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 0)

    def test_a_check_it_never_tried_is_tried_once(self):
        self.host.write({
            "metrics_agents_write_url": OFFER,
            "direct_reach_ok": True,
            "direct_reach_urls": OFFER,
        })
        extra = {
            "url": "https://direct.www.example.test:9443/oauth/jwks",
            "method": "GET", "status": 200, "prefix": "metrics_central_direct",
        }
        original = type(self.host)._direct_reach_probes
        with patch.object(
            type(self.host), "_direct_reach_probes", autospec=True,
            side_effect=lambda host, url: original(host, url) + [extra],
        ):
            self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 1)

    def test_the_cron_runs_the_convergence(self):
        self.host.metrics_agents_write_url = PUBLIC
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertEqual(self._queued(), 1)


class TestAHostGoneQuiet(_ConvergenceCase):

    def setUp(self):
        super().setUp()
        self.host.write({
            "metrics_agents_write_url": OFFER,
            "direct_reach_ok": True,
            "direct_reach_urls": OFFER,
            "metrics_last_seen": fields.Datetime.now() - timedelta(minutes=25),
        })

    def test_alone_and_quiet_for_25_minutes_it_is_re_applied(self):
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 1)

    def test_quiet_for_15_minutes_is_not_yet(self):
        self.host.metrics_last_seen = fields.Datetime.now() - timedelta(minutes=15)
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 0)

    def test_re_applied_40_minutes_ago_waits_for_the_hour(self):
        self.host.metrics_agents_since = (
            fields.Datetime.now() - timedelta(minutes=40)
        )
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 0)

    def test_a_central_not_read_for_15_minutes_re_applies_nobody(self):
        self.settings.metrics_read_ok_at = (
            fields.Datetime.now() - timedelta(minutes=15)
        )
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 0)

    def test_when_every_direct_host_is_quiet_nobody_is_re_applied(self):
        other = make_host(
            self.env, "cv-other", "46.4.0.92",
            metrics_agents_state="installed", metrics_agents_write_url=OFFER,
            metrics_last_seen=fields.Datetime.now() - timedelta(minutes=30),
        )
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 0)
        other.metrics_last_seen = fields.Datetime.now()
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertEqual(self._queued(), 1)


class TestANameInForceThatThisPanelDoesNotProbe(_ConvergenceCase):
    """A panel offered the name by someone else, as a tenant's is by its manager.

    Core's own state of the name stays ``unknown`` there: nobody on that
    panel probes it. Whether the name is in force is the layer's to say
    (``cloud.settings._direct_name_in_force``), so a host that fails its
    own try is refused for six hours instead of being re-applied at every
    reconciliation with nothing to show for it.
    """

    def _refused_once(self):
        """A failed try while another layer says the name works; then reconcile."""
        settings_cls = type(self.settings)
        self.settings.metrics_central_direct_state = "unknown"
        self.host.write({
            "metrics_agents_write_url": PUBLIC, "direct_refused_at": False,
        })
        with patch.object(
            settings_cls, "_direct_name_in_force", autospec=True, return_value=True,
        ), patch.object(
            settings_cls, "_direct_central_write_url", autospec=True,
            return_value=OFFER,
        ):
            self.assertEqual(self.host._metrics_remote_write_url(), OFFER)
            self.host._record_direct_reach([{
                "url": OFFER, "prefix": "metrics_central_direct",
                "expected": 401, "status": -1, "msg": "Connection refused",
            }])
            self.env["cloud.host"]._reconcile_direct_agents()

    def test_the_refusal_is_the_hosts_and_waits_six_hours(self):
        self._refused_once()
        self.assertTrue(self.host.direct_refused_at)
        self.assertEqual(self.host.direct_reach_failures, 1)
        self.assertEqual(self.host.direct_reach_reason, "connection")

    def test_the_reconciliation_does_not_re_apply_it(self):
        self._refused_once()
        self.assertEqual(self._queued(), 0)

    def test_core_says_in_force_from_the_probe_state(self):
        self.settings.metrics_central_direct_state = "ok"
        self.assertTrue(
            self.settings._direct_name_in_force("metrics_central_direct"),
        )
        self.settings.metrics_central_direct_state = "unknown"
        self.assertFalse(
            self.settings._direct_name_in_force("metrics_central_direct"),
        )
