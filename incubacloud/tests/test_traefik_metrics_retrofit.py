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


class TestTraefikAccessLogRetrofit(TransactionCase):
    """The access log followed the metrics block into the seed template and
    reached no existing host, because nothing amended the stored copies.
    Same three properties, same reasons."""

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]

    def test_adds_the_access_log_block(self):
        out = self.Host._add_traefik_access_log(_SEED_TRAEFIK)
        self.assertIn("accessLog:", out)
        self.assertIn("format: json", out)
        # Headers carry cookies and Authorization; dropping them is what
        # makes it safe to keep this on every host.
        self.assertIn("defaultMode: drop", out)

    def test_is_idempotent(self):
        once = self.Host._add_traefik_access_log(_SEED_TRAEFIK)
        twice = self.Host._add_traefik_access_log(once)
        self.assertEqual(once, twice)

    def test_leaves_a_customised_template_alone(self):
        custom = _SEED_TRAEFIK + "\naccessLog:\n  filePath: /var/log/own.log\n"
        self.assertEqual(self.Host._add_traefik_access_log(custom), custom)

    def test_preserves_existing_content(self):
        out = self.Host._add_traefik_access_log(_SEED_TRAEFIK)
        for line in ("sendAnonymousUsage: false", "dashboard: true",
                     'address: ":80"', 'address: ":443"'):
            self.assertIn(line, out)

    def test_handles_an_empty_template(self):
        self.assertFalse(self.Host._add_traefik_access_log(""))
        self.assertFalse(self.Host._add_traefik_access_log(False))


_SEED_CONFIG_OLD = """\
http:
  middlewares:
    buffering:
      buffering:
        retryExpression: IsNetworkError() && Attempts() < 5
    secure:
      headers:
        forceSTSHeader: "true"
        sslRedirect: "true"
    nocrawlers:
      headers:
        customResponseHeaders:
          X-Robots-Tag: "noindex, nofollow"
"""


class TestTraefikSecurityHeadersRetrofit(TransactionCase):
    """``secure`` gained its header set after the first hosts existed."""

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]

    def test_adds_the_missing_headers(self):
        out = self.Host._add_traefik_security_headers(_SEED_CONFIG_OLD)
        for key in ("stsSeconds", "stsIncludeSubdomains", "stsPreload",
                    "frameDeny", "contentTypeNosniff", "browserXssFilter",
                    "referrerPolicy"):
            self.assertIn(key, out)

    def test_adds_them_under_the_secure_middleware(self):
        """Anchored on ``secure``: another middleware must not be extended."""
        out = self.Host._add_traefik_security_headers(_SEED_CONFIG_OLD)
        secure = out.split("secure:", 1)[1].split("nocrawlers:", 1)[0]
        self.assertIn("frameDeny", secure)
        # ...and the indentation has to match, or the file stops parsing.
        self.assertIn('        frameDeny: "true"\n', out)

    def test_the_result_still_parses(self):
        import yaml

        out = self.Host._add_traefik_security_headers(_SEED_CONFIG_OLD)
        parsed = yaml.safe_load(out)
        headers = parsed["http"]["middlewares"]["secure"]["headers"]
        self.assertEqual(headers["stsSeconds"], 31536000)
        self.assertEqual(headers["referrerPolicy"],
                         "strict-origin-when-cross-origin")
        # The keys that were already there survive untouched.
        self.assertEqual(headers["forceSTSHeader"], "true")

    def test_is_idempotent(self):
        once = self.Host._add_traefik_security_headers(_SEED_CONFIG_OLD)
        twice = self.Host._add_traefik_security_headers(once)
        self.assertEqual(once, twice)

    def test_keeps_a_hosts_own_value(self):
        """An operator who shortened the HSTS window keeps their number."""
        own = _SEED_CONFIG_OLD.replace(
            '        sslRedirect: "true"\n',
            '        sslRedirect: "true"\n        stsSeconds: 60\n',
        )
        out = self.Host._add_traefik_security_headers(own)
        self.assertIn("stsSeconds: 60", out)
        self.assertNotIn("stsSeconds: 31536000", out)

    def test_skips_an_unrecognisable_middleware_block(self):
        odd = "http:\n  middlewares:\n    secure: {}\n"
        self.assertEqual(self.Host._add_traefik_security_headers(odd), odd)

    def test_handles_an_empty_template(self):
        self.assertFalse(self.Host._add_traefik_security_headers(""))
        self.assertFalse(self.Host._add_traefik_security_headers(False))
