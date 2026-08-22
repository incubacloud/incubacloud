"""Tests for the Traefik per-source-IP rate-limit retrofit (P1 / SEC-008).

Tenant domains resolve straight to their host, not through a CDN, so a
login flood reaches Odoo's pbkdf2 hasher directly — the asymmetric CPU
DoS of SEC-008, but against the tenant sites and unbounded. The defence
is a ``rateLimit`` middleware attached as an https-entrypoint default, so
it reaches every copier-generated tenant router without touching a single
generated compose file, exactly like the HSTS header.

The Traefik templates are stored **per host** and only filled when empty,
so editing the seed files reaches new hosts only. Existing hosts need the
stored copy amended in place, and that copy may have been hand-edited —
which is why the amendment is a minimal, interlocked merge and never a
regeneration. These pin the three properties that matter: it adds what is
missing, it is idempotent, and it never clobbers a customised template.
"""
import yaml

from odoo.tests.common import TransactionCase

from ..models.cloud_host import CloudHost, _read_traefik_template


_SEED_CONFIG = """\
http:
  middlewares:
    compress:
      compress: "true"
    secure:
      headers:
        forceSTSHeader: "true"
        frameDeny: "true"
"""

#: The https entrypoint as it looks on a host the HSTS retrofit already
#: reached: a ``middlewares`` list that carries ``hsts@file``. This is the
#: managed chain the rate-limit reference is allowed to extend.
_SEED_TRAEFIK = """\
global:
  sendAnonymousUsage: false

entryPoints:
  http:
    address: ":80"
  https:
    http:
      tls: "true"
      middlewares:
        - hsts@file
    address: ":443"

api:
  dashboard: true
"""

#: A dynamic config that already defines the middleware, so the entrypoint
#: retrofit is allowed to reference it.
_WITH_RL = CloudHost._add_traefik_ratelimit_middleware(_SEED_CONFIG)


class TestTraefikRateLimitRetrofit(TransactionCase):
    """The two edits that make Traefik throttle by client IP.

    They only work together — the middleware lives in the dynamic config,
    the reference to it on the https entrypoint in the static one — so both
    are pinned here.
    """

    # ── config.yml: the middleware ─────────────────────────────────────

    def test_middleware_is_added(self):
        out = CloudHost._add_traefik_ratelimit_middleware(_SEED_CONFIG)
        parsed = yaml.safe_load(out)
        self.assertEqual(
            parsed["http"]["middlewares"]["ratelimit"]["rateLimit"],
            {"average": 300, "period": "1m", "burst": 100},
        )

    def test_middleware_leaves_the_rest_alone(self):
        out = CloudHost._add_traefik_ratelimit_middleware(_SEED_CONFIG)
        parsed = yaml.safe_load(out)
        del parsed["http"]["middlewares"]["ratelimit"]
        self.assertEqual(parsed, yaml.safe_load(_SEED_CONFIG))

    def test_middleware_is_idempotent(self):
        once = CloudHost._add_traefik_ratelimit_middleware(_SEED_CONFIG)
        self.assertEqual(
            CloudHost._add_traefik_ratelimit_middleware(once), once,
        )

    def test_middleware_is_a_noop_on_unknown_shapes(self):
        for junk in ("", "http:\n  routers: {}\n", "nothing: here\n"):
            self.assertEqual(
                CloudHost._add_traefik_ratelimit_middleware(junk), junk,
            )

    # ── traefik.yml: the entrypoint reference ──────────────────────────

    def test_entrypoint_reference_is_appended(self):
        out = CloudHost._add_traefik_entrypoint_ratelimit(
            _SEED_TRAEFIK, _WITH_RL,
        )
        parsed = yaml.safe_load(out)
        self.assertEqual(
            parsed["entryPoints"]["https"]["http"]["middlewares"],
            ["hsts@file", "ratelimit@file"],
        )
        # The entrypoint keeps everything it had.
        self.assertEqual(parsed["entryPoints"]["https"]["address"], ":443")
        self.assertEqual(
            parsed["entryPoints"]["https"]["http"]["tls"], "true",
        )

    def test_entrypoint_reference_is_idempotent(self):
        once = CloudHost._add_traefik_entrypoint_ratelimit(
            _SEED_TRAEFIK, _WITH_RL,
        )
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_ratelimit(once, _WITH_RL), once,
        )

    def test_entrypoint_reference_needs_the_middleware_to_exist(self):
        """The pair fails closed.

        A reference to a middleware the file provider does not define does
        not lose a limit — it 500s every router of the entrypoint, which is
        the whole host. So no reference is written unless ``config.yml``
        defines ``ratelimit``.
        """
        without = "http:\n  routers: {}\n"
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_ratelimit(_SEED_TRAEFIK, without),
            _SEED_TRAEFIK,
        )
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_ratelimit(_SEED_TRAEFIK, ""),
            _SEED_TRAEFIK,
        )

    def test_entrypoint_leaves_a_foreign_chain_alone(self):
        """An operator's own chain is never appended to.

        The reference is added only to the list this project manages — the
        one HSTS marks with ``hsts@file``. A hand-written chain without it
        is left untouched, because appending to it blind is how a retrofit
        turns a working proxy into one that will not start.
        """
        foreign = _SEED_TRAEFIK.replace("        - hsts@file\n",
                                        "        - mine@file\n")
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_ratelimit(foreign, _WITH_RL),
            foreign,
        )

    def test_entrypoint_is_a_noop_without_a_middlewares_list(self):
        """No list to extend → nothing happens.

        HSTS creates the entrypoint ``middlewares`` list; this retrofit
        only appends to it. A host whose https entrypoint has no list yet
        is left for the HSTS retrofit to seed first.
        """
        no_list = _SEED_TRAEFIK.replace(
            "      middlewares:\n        - hsts@file\n", "",
        )
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_ratelimit(no_list, _WITH_RL),
            no_list,
        )

    # ── The seed files and the retrofit must agree ─────────────────────

    def test_shipped_templates_already_carry_it(self):
        """If they disagreed, every fresh host would be rewritten by the
        retrofit — or keep a shape it does not recognise and never get the
        limit."""
        config = _read_traefik_template("config.yml")
        traefik = _read_traefik_template("traefik.yml")
        # Both retrofits are no-ops on the shipped seed.
        self.assertEqual(
            CloudHost._add_traefik_ratelimit_middleware(config), config,
        )
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_ratelimit(traefik, config),
            traefik,
        )
        # The seed defines the middleware with the documented defaults …
        parsed_cfg = yaml.safe_load(config)
        self.assertEqual(
            parsed_cfg["http"]["middlewares"]["ratelimit"]["rateLimit"],
            {"average": 300, "period": "1m", "burst": 100},
        )
        # … and references it on the https entrypoint, after hsts.
        parsed = yaml.safe_load(traefik)
        self.assertEqual(
            parsed["entryPoints"]["https"]["http"]["middlewares"],
            ["hsts@file", "ratelimit@file"],
        )

    def test_reference_resolves_to_a_defined_middleware(self):
        """The two files agree on the name.

        An entrypoint that names a middleware the file provider does not
        define answers 500 on every router — the whole host — so the link
        is pinned rather than assumed.
        """
        traefik = yaml.safe_load(_read_traefik_template("traefik.yml"))
        config = yaml.safe_load(_read_traefik_template("config.yml"))
        referenced = traefik["entryPoints"]["https"]["http"]["middlewares"]
        defined = config["http"]["middlewares"]
        self.assertIn("ratelimit@file", referenced)
        for name in referenced:
            self.assertTrue(name.endswith("@file"))
            self.assertIn(name[: -len("@file")], defined)
