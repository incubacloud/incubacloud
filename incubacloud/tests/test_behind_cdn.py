"""What a host renders depends on whether a CDN answers for it.

The rate limit used to start keying on the forwarded chain as soon as
any trusted range was declared. That reads an absent header on a host
the world reaches directly, and Traefik then puts every one of its
visitors in a single bucket — which is how one flag shipped to the
wrong host turned a per-visitor limit into a shared 300/min cap for
fourteen instances.

Declaring ranges and being reached through them are separate facts, and
these pin that they stay separate.
"""
import yaml

from odoo.tests.common import TransactionCase


class TestBehindCdnRendering(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "cdn-render-host",
            "ip_address": "198.51.100.10",
            "user": "root",
            "wildcard_domain": "test.example",
        })
        self.ranges = ["203.0.113.0/24", "2001:db8::/32"]

    def _config(self, **kwargs):
        return self.host._patch_config_yml_trusted_proxies(
            self.host.traefik_config_yml, self.ranges, **kwargs,
        )

    def _rate_limit(self, **kwargs):
        """Parse the rendered document rather than grepping it.

        The template *documents* ``sourceCriterion.ipStrategy`` in a
        comment, so a substring check passes on a document that does not
        configure it at all.
        """
        return yaml.safe_load(
            self._config(**kwargs),
        )["http"]["middlewares"]["ratelimit"]["rateLimit"]

    def test_direct_host_keeps_the_address_keyed_limit(self):
        self.assertNotIn("sourceCriterion", self._rate_limit(behind_cdn=False))

    def test_host_behind_a_cdn_keys_on_the_forwarded_chain(self):
        self.assertEqual(
            self._rate_limit(behind_cdn=True)["sourceCriterion"],
            {"ipStrategy": {"depth": 1}},
        )

    def test_the_allowlist_does_not_depend_on_the_cdn_flag(self):
        """Refusing direct traffic is a separate decision from keying.

        A host can be told to refuse what did not come through its
        proxies while its rate limit still counts connecting addresses;
        conflating the two is what this whole field exists to undo.
        """
        rendered = self._config(block_direct=True, behind_cdn=False)
        parsed = yaml.safe_load(rendered)["http"]["middlewares"]
        self.assertIn("trusted-proxies-only", parsed)
        self.assertNotIn("sourceCriterion", parsed["ratelimit"]["rateLimit"])

    def test_the_shipped_document_follows_the_host_flag(self):
        self.host.behind_cdn = False
        self.host.trusted_proxy_ranges = "\n".join(self.ranges)
        limit = yaml.safe_load(
            self.host._shipped_config_yml(),
        )["http"]["middlewares"]["ratelimit"]["rateLimit"]
        self.assertNotIn("sourceCriterion", limit)
        self.host.behind_cdn = True
        limit = yaml.safe_load(
            self.host._shipped_config_yml(),
        )["http"]["middlewares"]["ratelimit"]["rateLimit"]
        self.assertIn("sourceCriterion", limit)

    def test_the_flag_is_part_of_the_drift_snapshot(self):
        """Otherwise the panel cannot say the host has not been told."""
        self.assertIn("behind_cdn", self.host._config_snapshot_fields())


class TestDefaultCertificate(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "cert-host",
            "ip_address": "198.51.100.11",
            "user": "root",
            "wildcard_domain": "test.example",
        })

    def test_no_certificate_renders_no_document(self):
        """An empty document is the signal to remove the file.

        A TLS store naming files that are not on the host makes Traefik
        answer every handshake with its own throwaway certificate, which
        looks like a certificate problem and is a configuration one.
        """
        self.assertEqual(self.host._shipped_tls_default_yml(), "")

    def test_half_a_pair_renders_nothing(self):
        self.host.tls_default_cert = "-----BEGIN CERTIFICATE-----"
        self.assertEqual(self.host._shipped_tls_default_yml(), "")

    def test_a_full_pair_names_both_files(self):
        self.host.write({
            "tls_default_cert": "-----BEGIN CERTIFICATE-----",
            "tls_default_key": "-----BEGIN PRIVATE KEY-----",
        })
        rendered = self.host._shipped_tls_default_yml()
        self.assertIn("defaultCertificate", rendered)
        self.assertIn("/etc/certs/default.crt", rendered)
        self.assertIn("/etc/certs/default.key", rendered)

    def test_the_key_is_not_stored_in_the_clear(self):
        self.host.write({
            "tls_default_cert": "-----BEGIN CERTIFICATE-----",
            "tls_default_key": "-----BEGIN PRIVATE KEY-----secret",
        })
        self.env.cr.execute(
            "SELECT tls_default_key FROM cloud_host WHERE id = %s",
            (self.host.id,),
        )
        stored = self.env.cr.fetchone()[0]
        self.assertNotIn("secret", stored or "")

    def test_the_certificate_is_part_of_the_drift_snapshot(self):
        self.assertIn("tls_default_cert", self.host._config_snapshot_fields())


class TestFirewallAllowlistVars(TransactionCase):
    """The firewall half of refusing direct traffic.

    Behind a CDN every visitor arrives from a handful of edge addresses,
    so the per-source connection cap would throttle the CDN and protect
    nobody. The cap is replaced by an explicit set — but only once the
    host is also refusing direct traffic at the proxy, so the two halves
    cannot be enabled in an order that leaves the host answering nobody.
    """

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "fw-host",
            "ip_address": "198.51.100.12",
            "user": "root",
            "wildcard_domain": "test.example",
            "trusted_proxy_ranges": "203.0.113.0/24\n2001:db8::/32",
        })
        self.executor = self.env["cloud.job"]

    def _vars(self):
        from odoo.addons.incubacloud.models.host_hardening_executor import (
            HostHardeningExecutor,
        )
        job = self.env["cloud.job"].new({"host_id": self.host.id})
        executor = HostHardeningExecutor.__new__(HostHardeningExecutor)
        executor.job = job
        return executor._http_allowlist_vars()

    def test_a_direct_host_gets_no_allowlist(self):
        self.host.write({"behind_cdn": False, "block_direct_access": False})
        self.assertEqual(self._vars(), {})

    def test_behind_a_cdn_but_still_accepting_direct_gets_none(self):
        self.host.write({"behind_cdn": True, "block_direct_access": False})
        self.assertEqual(self._vars(), {})

    def test_both_flags_split_the_ranges_by_family(self):
        self.host.write({"behind_cdn": True, "block_direct_access": True})
        result = self._vars()
        self.assertEqual(result["ic_http_allowed_ranges_v4"], "203.0.113.0/24")
        self.assertEqual(result["ic_http_allowed_ranges_v6"], "2001:db8::/32")
