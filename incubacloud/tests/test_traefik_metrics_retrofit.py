"""Tests for the Traefik metrics retrofit (Fase 4 / A4).

The Traefik templates are stored **per host** and only ever filled when
empty, so editing the seed files in this repo reaches new hosts only.
Existing hosts need their stored copy amended in place — and that copy
may have been hand-edited, which is why the amendment is a minimal merge
and never a regeneration.

These pin the three properties that matter: it adds what is missing, it
is idempotent, and it does not clobber a customised template.
"""
from odoo.tests.common import TransactionCase


_SEED_TRAEFIK = """\
global:
  sendAnonymousUsage: false

entryPoints:
  http:
    address: ":80"
  https:
    address: ":443"

api:
  dashboard: true
"""

_SEED_INVERSEPROXY = """\
services:
  proxy:
    image: traefik:v2.11
    ports:
      - "80:80"
      - "443:443"
    restart: unless-stopped
"""


class TestTraefikMetricsRetrofit(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]

    # ── traefik.yml ────────────────────────────────────────────────────

    def test_adds_the_metrics_block_and_entrypoint(self):
        out = self.Host._add_traefik_metrics(_SEED_TRAEFIK)
        self.assertIn("metrics:\n  prometheus:", out)
        self.assertIn("entryPoint: metrics", out)
        self.assertIn('address: ":8082"', out)
        # Per-router labels are the whole point: without them there are no
        # per-instance HTTP metrics.
        self.assertIn("addRoutersLabels: true", out)

    def test_is_idempotent(self):
        once = self.Host._add_traefik_metrics(_SEED_TRAEFIK)
        twice = self.Host._add_traefik_metrics(once)
        self.assertEqual(once, twice)

    def test_leaves_a_customised_template_alone(self):
        """A host that already configures metrics keeps its own config."""
        custom = _SEED_TRAEFIK + "\nmetrics:\n  prometheus:\n    entryPoint: own\n"
        self.assertEqual(self.Host._add_traefik_metrics(custom), custom)

    def test_preserves_existing_content(self):
        """The merge adds; it must not rewrite what was there."""
        out = self.Host._add_traefik_metrics(_SEED_TRAEFIK)
        for line in ("sendAnonymousUsage: false", "dashboard: true",
                     'address: ":80"', 'address: ":443"'):
            self.assertIn(line, out)

    def test_handles_an_empty_template(self):
        self.assertFalse(self.Host._add_traefik_metrics(""))
        self.assertFalse(self.Host._add_traefik_metrics(False))

    # ── inverseproxy.yaml ──────────────────────────────────────────────

    def test_publishes_the_metrics_port_on_loopback(self):
        out = self.Host._add_traefik_metrics_port(_SEED_INVERSEPROXY)
        self.assertIn('- "127.0.0.1:8082:8082"', out)
        # Loopback only: the metrics endpoint is unauthenticated, so it
        # must never be reachable from off-box.
        self.assertNotIn('- "8082:8082"', out)

    def test_port_retrofit_is_idempotent(self):
        once = self.Host._add_traefik_metrics_port(_SEED_INVERSEPROXY)
        twice = self.Host._add_traefik_metrics_port(once)
        self.assertEqual(once, twice)

    def test_port_retrofit_skips_an_unrecognisable_ports_block(self):
        """When the shape is not what we expect, do nothing rather than guess."""
        odd = "services:\n  proxy:\n    image: traefik:v2.11\n"
        self.assertEqual(self.Host._add_traefik_metrics_port(odd), odd)

    def test_port_retrofit_keeps_the_existing_ports(self):
        out = self.Host._add_traefik_metrics_port(_SEED_INVERSEPROXY)
        self.assertIn('- "80:80"', out)
        self.assertIn('- "443:443"', out)

    # ── Applied to a real host ─────────────────────────────────────────

    def test_init_retrofits_an_existing_host(self):
        """A host stored before this feature gets the wiring on upgrade."""
        host = self.Host.create({
            "name": "legacy",
            "ip_address": "10.9.9.9",
            "user": "root",
            "wildcard_domain": "legacy.example.com",
            "traefik_yml": _SEED_TRAEFIK,
            "traefik_inverseproxy_yaml": _SEED_INVERSEPROXY,
            "traefik_config_yml": "http: {}\n",
        })
        self.Host.init_traefik_templates()
        host.invalidate_recordset()
        self.assertIn("prometheus:", host.traefik_yml)
        self.assertIn('127.0.0.1:8082:8082', host.traefik_inverseproxy_yaml)
