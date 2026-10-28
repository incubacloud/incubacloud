"""What a host with a direct port ships to its proxy.

The static file gains the ``direct`` entrypoint and the ``direct-dns``
resolver; the compose file publishes the port on IPv4 and, when the host
needs DNS-01 and the token is stored, mounts it as a file. Without a
port, nothing of it is rendered: the file comes out as before.
"""
import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_host_direct import (
    DIRECT_MARK,
    patch_inverseproxy_direct,
    patch_traefik_yml_direct,
)
from odoo.addons.incubacloud.tests._direct import make_host

_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


class TestTheStaticFile(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = make_host(self.env, "dr-host", "46.4.0.40")

    def _static(self, port=9443):
        self.host.direct_port = port
        return yaml.safe_load(self.host._shipped_traefik_yml())

    def test_the_entrypoint_trusts_no_forwarded_header(self):
        direct = self._static()["entryPoints"]["direct"]
        self.assertEqual(direct["address"], ":9443")
        self.assertNotIn("forwardedHeaders", direct)
        self.assertEqual(direct["http"]["tls"], {})
        self.assertEqual(
            direct["http"]["middlewares"],
            ["direct-peers@file", "direct-strip@file", "direct-ratelimit@file"],
        )

    def test_the_entrypoint_has_its_own_timeouts_and_streams(self):
        static = self._static()
        direct = static["entryPoints"]["direct"]
        self.assertEqual(
            direct["transport"]["respondingTimeouts"],
            {"readTimeout": "30s", "writeTimeout": "60s", "idleTimeout": "90s"},
        )
        self.assertEqual(direct["http2"]["maxConcurrentStreams"], 50)
        self.assertNotIn("transport", static["entryPoints"]["https"])

    def test_the_resolver_asks_by_dns_with_the_letsencrypt_account(self):
        static = self._static()
        email = static["certificatesResolvers"]["letsencrypt"]["acme"]["email"]
        acme = static["certificatesResolvers"]["direct-dns"]["acme"]
        self.assertEqual(acme["email"], email)
        self.assertEqual(acme["storage"], "/etc/traefik/acme/acme.json")
        self.assertEqual(acme["dnsChallenge"]["provider"], "cloudflare")
        self.assertNotIn("caServer", acme)

    def test_a_staging_letsencrypt_makes_a_staging_direct_dns(self):
        self.host.traefik_yml = self.host.traefik_yml.replace(
            f'#caServer: "{_STAGING}"', f'caServer: "{_STAGING}"',
        )
        acme = self._static()["certificatesResolvers"]["direct-dns"]["acme"]
        self.assertEqual(acme["caServer"], _STAGING)

    def test_without_a_port_nothing_is_rendered(self):
        before = self._static(port=0)
        rendered = self.host._shipped_traefik_yml()
        self.assertNotIn(DIRECT_MARK, rendered)
        self.assertNotIn("direct", before["entryPoints"])
        self.assertNotIn("direct-dns", before["certificatesResolvers"])
        self._static(port=9443)
        self.host.direct_port = 0
        self.assertEqual(self.host._shipped_traefik_yml(), rendered)

    def test_rendering_twice_adds_nothing(self):
        once = patch_traefik_yml_direct(self.host.traefik_yml, 9443)
        self.assertEqual(patch_traefik_yml_direct(once, 9443), once)


class TestTheComposeFile(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = make_host(self.env, "dc-host", "46.4.0.41")
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.acme_dns_cloudflare_token = "cf-dns-token-value"

    def _proxy(self):
        rendered = self.host._shipped_inverseproxy_yaml()
        return yaml.safe_load(rendered)["services"]["proxy"]

    def test_the_port_is_published_on_ipv4_only(self):
        self.host.direct_port = 9443
        self.assertIn("0.0.0.0:9443:9443", self._proxy()["ports"])

    def test_the_token_is_a_file_and_never_its_value(self):
        self.host.direct_port = 9443
        rendered = self.host._shipped_inverseproxy_yaml()
        proxy = yaml.safe_load(rendered)["services"]["proxy"]
        self.assertIn("./secrets:/etc/traefik/secrets:ro", proxy["volumes"])
        self.assertEqual(
            proxy["environment"]["CF_DNS_API_TOKEN_FILE"],
            "/etc/traefik/secrets/cf-dns-api-token",
        )
        self.assertNotIn("cf-dns-token-value", rendered)
        self.assertNotIn("CF_DNS_API_TOKEN:", rendered)

    def test_without_a_token_no_mount_and_no_variable(self):
        self.settings.acme_dns_cloudflare_token = False
        self.host.direct_port = 9443
        proxy = self._proxy()
        self.assertIn("0.0.0.0:9443:9443", proxy["ports"])
        self.assertNotIn("environment", proxy)
        self.assertFalse([v for v in proxy["volumes"] if "secrets" in v])

    def test_without_a_port_the_file_is_as_before(self):
        rendered = self.host._shipped_inverseproxy_yaml()
        self.assertNotIn("9443", rendered)
        self.assertNotIn("secrets", rendered)

    def test_an_environment_already_there_takes_the_variable(self):
        compose = (
            "services:\n  proxy:\n    image: traefik:v2.11\n"
            "    environment:\n      TZ: UTC\n    volumes:\n"
            "      - ./traefik.yml:/etc/traefik/traefik.yml:ro\n"
        )
        proxy = yaml.safe_load(
            patch_inverseproxy_direct(compose, 9443, True),
        )["services"]["proxy"]
        self.assertEqual(proxy["environment"]["TZ"], "UTC")
        self.assertEqual(
            proxy["environment"]["CF_DNS_API_TOKEN_FILE"],
            "/etc/traefik/secrets/cf-dns-api-token",
        )
        once = patch_inverseproxy_direct(compose, 9443, True)
        self.assertEqual(patch_inverseproxy_direct(once, 9443, True), once)
