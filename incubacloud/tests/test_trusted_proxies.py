"""Client identity at the edge, and the Traefik settings that establish it.

Measured against Traefik v2.11 before these were written: with no
``forwardedHeaders.trustedIPs`` declared, Traefik discards every inbound
``X-Forwarded-For`` and a middleware reading the forwarded chain matches
nothing — an allowlist built that way rejects 100% of traffic. With the
proxy declared, the chain is readable and a request that did not come
through it still cannot forge one. Both halves are pinned here.
"""
import yaml

from odoo.tests.common import TransactionCase

from ..models.cloud_host import CloudHost, _read_traefik_template
from ..net.trusted_proxies import client_ip, is_trusted, parse_ranges

#: Stand-ins for a CDN's published ranges, one per family.
EDGE = ["198.51.100.0/24", "2001:db8:beef::/48"]


class TestTrustedProxyParsing(TransactionCase):
    """The pure resolution rules, independent of any Traefik document."""

    def test_parse_accepts_newlines_and_commas(self):
        self.assertEqual(
            parse_ranges("10.0.0.0/8,\n 192.0.2.0/24 \n"),
            ["10.0.0.0/8", "192.0.2.0/24"],
        )

    def test_parse_drops_what_is_not_a_network(self):
        # A typo in an operator-entered field must not take out the
        # request path that reads it.
        self.assertEqual(parse_ranges("nonsense\n192.0.2.0/24"), ["192.0.2.0/24"])

    def test_parse_normalises_a_host_bit(self):
        self.assertEqual(parse_ranges("192.0.2.5/24"), ["192.0.2.0/24"])

    def test_parse_of_nothing_is_empty(self):
        self.assertEqual(parse_ranges(None), [])
        self.assertEqual(parse_ranges(""), [])

    def test_is_trusted_covers_both_families(self):
        self.assertTrue(is_trusted("198.51.100.7", EDGE))
        self.assertTrue(is_trusted("2001:db8:beef::1", EDGE))
        self.assertFalse(is_trusted("203.0.113.9", EDGE))
        self.assertFalse(is_trusted("not-an-address", EDGE))

    def test_with_no_declared_proxy_the_header_is_ignored(self):
        # The pre-existing behaviour, and the safe one: over-restrictive
        # rather than spoofable.
        self.assertEqual(
            client_ip("198.51.100.7", "203.0.113.9", []),
            "198.51.100.7",
        )

    def test_through_the_proxy_the_client_is_read(self):
        self.assertEqual(
            client_ip("198.51.100.7", "203.0.113.9, 198.51.100.7", EDGE),
            "203.0.113.9",
        )

    def test_a_forged_entry_left_of_the_proxy_is_not_read(self):
        # What the edge appended is the address it actually saw, and it
        # sits to the right of anything the caller wrote.
        self.assertEqual(
            client_ip(
                "198.51.100.7", "1.1.1.1, 203.0.113.9, 198.51.100.7", EDGE,
            ),
            "203.0.113.9",
        )

    def test_a_direct_caller_cannot_choose_its_own_identity(self):
        self.assertEqual(
            client_ip("203.0.113.9", "1.1.1.1", EDGE), "203.0.113.9",
        )

    def test_a_direct_caller_with_no_header_is_itself(self):
        self.assertEqual(client_ip("203.0.113.9", "", EDGE), "203.0.113.9")

    def test_a_chain_entirely_of_ours_falls_back_to_the_connection(self):
        # A health check from the proxy itself, for instance.
        self.assertEqual(
            client_ip("198.51.100.7", "198.51.100.8", EDGE), "198.51.100.7",
        )

    def test_the_chain_may_be_given_as_a_list(self):
        self.assertEqual(
            client_ip("198.51.100.7", ["203.0.113.9", "198.51.100.7"], EDGE),
            "203.0.113.9",
        )


class TestTraefikStaticRetrofit(TransactionCase):
    """``traefik.yml``: who Traefik believes, and who it refuses."""

    def setUp(self):
        super().setUp()
        self.template = _read_traefik_template("traefik.yml")

    def _patched(self, ranges=EDGE, block=False):
        return CloudHost._patch_traefik_yml_trusted_proxies(
            self.template, ranges, block,
        )

    def test_both_public_entrypoints_declare_the_ranges(self):
        parsed = yaml.safe_load(self._patched())["entryPoints"]
        for name in ("http", "https"):
            self.assertEqual(
                parsed[name]["forwardedHeaders"]["trustedIPs"], EDGE,
                f"{name} entrypoint must believe the declared proxies",
            )

    def test_the_metrics_entrypoint_is_left_alone(self):
        parsed = yaml.safe_load(self._patched())["entryPoints"]
        self.assertNotIn("forwardedHeaders", parsed["metrics"])

    def test_nothing_else_in_the_document_moves(self):
        before = yaml.safe_load(self.template)
        after = yaml.safe_load(self._patched())
        for key in ("providers", "accessLog", "metrics", "certificatesResolvers"):
            self.assertEqual(before[key], after[key])

    def test_re_rendering_replaces_rather_than_appends(self):
        # The ranges move. A retrofit that only ever added would leave a
        # host trusting a network the CDN gave up.
        once = self._patched()
        twice = CloudHost._patch_traefik_yml_trusted_proxies(
            once, ["203.0.113.0/24"],
        )
        self.assertEqual(
            yaml.safe_load(twice)["entryPoints"]["https"]["forwardedHeaders"],
            {"trustedIPs": ["203.0.113.0/24"]},
        )

    def test_rendering_the_same_ranges_twice_changes_nothing(self):
        once = self._patched()
        self.assertEqual(
            CloudHost._patch_traefik_yml_trusted_proxies(once, EDGE), once,
        )

    def test_clearing_the_ranges_restores_the_original(self):
        self.assertEqual(
            CloudHost._patch_traefik_yml_trusted_proxies(self._patched(), []),
            self.template,
        )

    def test_blocking_direct_access_runs_before_the_rest(self):
        chain = yaml.safe_load(
            self._patched(block=True),
        )["entryPoints"]["https"]["http"]["middlewares"]
        self.assertEqual(chain[0], "trusted-proxies-only@file")
        self.assertIn("hsts@file", chain)
        self.assertIn("ratelimit@file", chain)

    def test_blocking_direct_access_needs_a_proxy_to_trust(self):
        # With no range, this would refuse every visitor the host has.
        self.assertEqual(self._patched(ranges=[], block=True), self.template)

    def test_clearing_the_ranges_also_removes_the_refusal(self):
        blocked = self._patched(block=True)
        self.assertEqual(
            CloudHost._patch_traefik_yml_trusted_proxies(blocked, []),
            self.template,
        )

    def test_an_empty_document_is_returned_untouched(self):
        self.assertEqual(
            CloudHost._patch_traefik_yml_trusted_proxies("", EDGE), "",
        )

    def test_an_unrecognised_document_is_returned_untouched(self):
        stranger = "whatever: true\n"
        self.assertEqual(
            CloudHost._patch_traefik_yml_trusted_proxies(stranger, EDGE),
            stranger,
        )

    def test_a_hand_written_middleware_chain_is_not_extended(self):
        # Same stance as the HSTS and rate-limit retrofits: only the
        # chain we manage carries hsts@file.
        theirs = self.template.replace("- hsts@file\n", "- theirs@file\n")
        patched = CloudHost._patch_traefik_yml_trusted_proxies(
            theirs, EDGE, True,
        )
        self.assertNotIn("trusted-proxies-only@file", patched)


class TestTraefikDynamicRetrofit(TransactionCase):
    """``config.yml``: what the rate limit counts, and what refuses."""

    def setUp(self):
        super().setUp()
        self.template = _read_traefik_template("config.yml")

    def _patched(self, ranges=EDGE, block=False):
        return CloudHost._patch_config_yml_trusted_proxies(
            self.template, ranges, block,
        )

    def test_the_rate_limit_keys_on_the_forwarded_client(self):
        limit = yaml.safe_load(
            self._patched(),
        )["http"]["middlewares"]["ratelimit"]["rateLimit"]
        self.assertEqual(
            limit["sourceCriterion"], {"ipStrategy": {"depth": 1}},
        )
        # The limits themselves must not have moved.
        self.assertEqual(limit["average"], 300)
        self.assertEqual(limit["burst"], 100)

    def test_without_a_proxy_the_rate_limit_is_left_alone(self):
        # Measured: a forwarded-chain strategy with no header present
        # keys every direct visitor under the same empty value, which is
        # one shared bucket for the whole internet.
        self.assertEqual(self._patched(ranges=[]), self.template)

    def test_the_refusal_middleware_appears_only_when_asked_for(self):
        without = yaml.safe_load(self._patched())["http"]["middlewares"]
        self.assertNotIn("trusted-proxies-only", without)
        with_block = yaml.safe_load(
            self._patched(block=True),
        )["http"]["middlewares"]
        self.assertEqual(
            with_block["trusted-proxies-only"],
            {"ipWhiteList": {"sourceRange": EDGE}},
        )

    def test_the_refusal_reads_the_connection_not_the_header(self):
        # Reading the forwarded chain here would let a caller hand us
        # the very value being checked.
        allow = yaml.safe_load(
            self._patched(block=True),
        )["http"]["middlewares"]["trusted-proxies-only"]["ipWhiteList"]
        self.assertNotIn("ipStrategy", allow)

    def test_the_other_middlewares_are_untouched(self):
        before = yaml.safe_load(self.template)["http"]["middlewares"]
        after = yaml.safe_load(self._patched(block=True))["http"]["middlewares"]
        for name in ("hsts", "doodba", "compress", "localhost-only"):
            self.assertEqual(before[name], after[name])

    def test_rendering_twice_changes_nothing(self):
        once = self._patched(block=True)
        self.assertEqual(
            CloudHost._patch_config_yml_trusted_proxies(once, EDGE, True), once,
        )

    def test_re_rendering_replaces_the_ranges(self):
        once = self._patched(block=True)
        twice = CloudHost._patch_config_yml_trusted_proxies(
            once, ["203.0.113.0/24"], True,
        )
        allow = yaml.safe_load(twice)["http"]["middlewares"]
        self.assertEqual(
            allow["trusted-proxies-only"]["ipWhiteList"]["sourceRange"],
            ["203.0.113.0/24"],
        )

    def test_clearing_the_ranges_restores_the_original(self):
        self.assertEqual(
            CloudHost._patch_config_yml_trusted_proxies(
                self._patched(block=True), [],
            ),
            self.template,
        )

    def test_an_unrecognised_document_is_returned_untouched(self):
        stranger = "whatever: true\n"
        self.assertEqual(
            CloudHost._patch_config_yml_trusted_proxies(stranger, EDGE, True),
            stranger,
        )


class TestHostShipsWhatItDeclares(TransactionCase):
    """The host record, and what a setup run would actually upload."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "proxy-host",
            "ip_address": "192.0.2.50",
            "user": "ubuntu",
            "wildcard_domain": "proxy.example.com",
        })

    def test_a_host_declares_nothing_by_default(self):
        self.assertEqual(self.host._effective_trusted_proxy_ranges(), [])

    def test_a_plain_host_ships_its_templates_unchanged(self):
        self.assertEqual(
            self.host._shipped_traefik_yml(), self.host.traefik_yml,
        )
        self.assertEqual(
            self.host._shipped_config_yml(), self.host.traefik_config_yml,
        )

    def test_declared_ranges_reach_both_shipped_documents(self):
        self.host.trusted_proxy_ranges = "\n".join(EDGE)
        static = yaml.safe_load(self.host._shipped_traefik_yml())
        self.assertEqual(
            static["entryPoints"]["https"]["forwardedHeaders"]["trustedIPs"],
            EDGE,
        )
        dynamic = yaml.safe_load(self.host._shipped_config_yml())
        self.assertIn(
            "sourceCriterion",
            dynamic["http"]["middlewares"]["ratelimit"]["rateLimit"],
        )

    def test_the_interlock_holds_when_direct_access_is_refused(self):
        # A host whose entrypoint names a middleware its file provider
        # does not define answers 500 on every router it serves, so the
        # two documents have to be rendered together.
        self.host.write({
            "trusted_proxy_ranges": "\n".join(EDGE),
            "block_direct_access": True,
        })
        static = yaml.safe_load(self.host._shipped_traefik_yml())
        dynamic = yaml.safe_load(self.host._shipped_config_yml())
        referenced = static["entryPoints"]["https"]["http"]["middlewares"]
        self.assertIn("trusted-proxies-only@file", referenced)
        self.assertIn("trusted-proxies-only", dynamic["http"]["middlewares"])

    def test_refusing_direct_access_alone_changes_nothing(self):
        self.host.block_direct_access = True
        self.assertEqual(
            self.host._shipped_traefik_yml(), self.host.traefik_yml,
        )
        self.assertEqual(
            self.host._shipped_config_yml(), self.host.traefik_config_yml,
        )

    def test_the_settings_are_part_of_the_deployed_snapshot(self):
        # Otherwise the panel would report a host as up to date while
        # carrying a proxy posture the host has never been told about.
        fields = self.host._config_snapshot_fields()
        self.assertIn("trusted_proxy_ranges", fields)
        self.assertIn("block_direct_access", fields)
        before = self.host._config_snapshot_hash()
        self.host.trusted_proxy_ranges = "\n".join(EDGE)
        self.assertNotEqual(before, self.host._config_snapshot_hash())
