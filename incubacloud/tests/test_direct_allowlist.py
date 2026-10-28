"""Who the direct port admits, and the document Traefik reads it from.

``direct-hosts`` is the platform's hosts by their own public address plus
the private ranges (traffic born on the host itself crosses the forward
hook with its container's address). ``direct-peers`` adds what a layer
above admits besides: the servers customers rent, in the SaaS build.
A host that may not be ours any more is left out: archived, absent from
the metrics for over a day, or (SaaS) bought on demand and without a
server. An address counts as applied only by its own /32.
"""
import ipaddress
from datetime import timedelta
from unittest.mock import patch

import yaml

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_host_direct import (
    DIRECT_PRIVATE_RANGES,
    global_ipv4,
)
from odoo.addons.incubacloud.tests._direct import make_host


class TestTheDirectList(TransactionCase):

    def setUp(self):
        super().setUp()
        self.web = make_host(self.env, "dl-web", "46.4.0.20")
        self.other = make_host(self.env, "dl-other", "46.4.0.21")

    def _hosts(self):
        return self.env["cloud.host"]._direct_hosts_entries()[0]

    def test_a_global_ipv4_is_the_only_kind_of_address(self):
        self.assertEqual(global_ipv4(" 46.4.0.1 "), "46.4.0.1")
        for value in ("0.0.0.0", "10.1.2.3", "100.64.0.1", "192.0.2.1",
                      "2a01:4f8::1", "server.example.com", "", False):
            with self.subTest(value=value):
                self.assertEqual(global_ipv4(value), "")

    def test_active_hosts_by_their_own_address_and_the_private_ranges(self):
        entries = self._hosts()
        self.assertIn("46.4.0.20/32", entries)
        self.assertIn("46.4.0.21/32", entries)
        for private in DIRECT_PRIVATE_RANGES:
            self.assertIn(private, entries)

    def test_what_cannot_be_a_platform_address_stays_out(self):
        for name, ip in (
            ("dl-zero", "0.0.0.0"), ("dl-private", "10.1.2.3"),
            ("dl-v6", "2a01:4f8::1"), ("dl-name", "server.example.com"),
            ("dl-shared", "100.64.0.1"),
        ):
            make_host(self.env, name, ip)
        entries = self._hosts()
        for absent in ("0.0.0.0/32", "10.1.2.3/32", "100.64.0.1/32"):
            self.assertNotIn(absent, entries)
        self.assertFalse([e for e in entries if ":" in e or "example" in e])

    def test_an_archived_host_leaves_and_a_shared_address_is_one_entry(self):
        make_host(self.env, "dl-twin", "46.4.0.20")
        gone = make_host(self.env, "dl-gone", "46.4.0.22")
        gone.active = False
        entries = self._hosts()
        self.assertEqual(entries.count("46.4.0.20/32"), 1)
        self.assertNotIn("46.4.0.22/32", entries)

    def test_the_peers_are_sorted_and_without_duplicates(self):
        entries = self.env["cloud.host"]._direct_peers_entries()
        self.assertEqual(entries, sorted(set(entries), key=ipaddress.ip_network))

    def _absent_since(self, hours):
        """Raise ``metrics_host_absent`` on dl-other, *hours* ago."""
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "metrics_host_absent", "no metrics", host=self.other,
            confirm_after=1, confirm_window=900,
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_alert SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(hours=hours), alert.id),
        )
        alert.invalidate_recordset(["create_date"])
        return alert

    def test_a_host_absent_for_a_day_leaves_and_comes_back(self):
        alert = self._absent_since(25)
        entries, left_out = self.env["cloud.host"]._direct_hosts_entries()
        self.assertNotIn("46.4.0.21/32", entries)
        self.assertIn("dl-other", left_out)
        alert.write({"state": "dismissed"})
        self.assertIn("46.4.0.21/32", self._hosts())

    def test_an_absence_of_23_hours_does_not_count(self):
        self._absent_since(23)
        self.assertIn("46.4.0.21/32", self._hosts())

    def test_an_address_is_applied_by_its_own_32_only(self):
        host = make_host(
            self.env, "dl-pub", "46.4.0.23",
            direct_allowlist_shipped="10.0.0.0/8\n46.4.0.20/32",
        )
        self.assertTrue(host._direct_admits("46.4.0.20"))
        self.assertFalse(host._direct_admits("10.1.2.3"))
        self.assertFalse(host._direct_admits("46.4.0.24"))


class TestTheDirectDocument(TransactionCase):

    def setUp(self):
        super().setUp()
        make_host(self.env, "dd-web", "46.4.0.30")
        self.Host = self.env["cloud.host"]
        self.doc = yaml.safe_load(self.Host._shipped_direct_yml())
        self.mws = self.doc["http"]["middlewares"]

    def test_peers_and_hosts_are_the_two_lists(self):
        hosts = self.mws["direct-hosts"]["ipWhiteList"]["sourceRange"]
        peers = self.mws["direct-peers"]["ipWhiteList"]["sourceRange"]
        self.assertIn("46.4.0.30/32", hosts)
        self.assertLessEqual(set(hosts), set(peers))
        self.assertEqual(hosts, self.Host._direct_hosts_entries()[0])
        self.assertEqual(peers, self.Host._direct_peers_entries())

    def test_the_extras_join_the_peers_only(self):
        with patch.object(
            type(self.Host), "_direct_extra_peers",
            autospec=True, return_value=["46.4.0.31/32"],
        ):
            mws = yaml.safe_load(self.Host._shipped_direct_yml())["http"]["middlewares"]
        peers = mws["direct-peers"]["ipWhiteList"]["sourceRange"]
        hosts = mws["direct-hosts"]["ipWhiteList"]["sourceRange"]
        self.assertIn("46.4.0.31/32", peers)
        self.assertNotIn("46.4.0.31/32", hosts)

    def test_everything_else_is_refused_at_priority_10000(self):
        router = self.doc["http"]["routers"]["direct-deny"]
        self.assertEqual(router["rule"], "PathPrefix(`/`)")
        self.assertEqual(router["priority"], 10000)
        self.assertEqual(router["entryPoints"], ["direct"])
        self.assertEqual(router["middlewares"], ["direct-deny-all"])
        self.assertEqual(router["service"], "noop@internal")
        self.assertEqual(router["tls"], {})
        self.assertEqual(
            self.mws["direct-deny-all"]["ipWhiteList"]["sourceRange"], ["127.0.0.2/32"],
        )

    def test_no_client_certificate_and_no_forwarded_identity(self):
        self.assertEqual(
            self.doc["tls"]["options"]["direct"], {"minVersion": "VersionTLS12"},
        )
        stripped = self.mws["direct-strip"]["headers"]["customRequestHeaders"]
        self.assertEqual(
            stripped,
            {"CF-Connecting-IP": "", "CF-Connecting-IPv6": "", "True-Client-IP": ""},
        )
        self.assertEqual(
            self.mws["direct-ratelimit"]["rateLimit"],
            {"average": 600, "period": "1m", "burst": 300},
        )

    def test_two_renders_are_the_same(self):
        first = self.Host._shipped_direct_yml()
        self.assertEqual(first, self.Host._shipped_direct_yml())
