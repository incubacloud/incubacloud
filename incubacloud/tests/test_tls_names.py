"""Whether a router asks a CA for a certificate, or serves the one held.

Getting this wrong is silent in both directions, which is why it is
pinned this closely.

Asking a CA for a name that now answers through a CDN can never
succeed: the challenge reaches the CDN, not the host. Nothing fails
loudly — the proxy keeps serving the certificate it already obtained
until that one expires, and only then starts handing out a throwaway.

Serving the held certificate for a name that is still reached directly
is worse and faster: a certificate issued for the CDN's benefit is
trusted by the CDN, and by no browser. Visitors get a warning page the
moment it is deployed.

So the two conditions have to be tested together, not one and then the
other.
"""
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.net import tls_names

from ._certs import make_pair


class TestNormalising(BaseCase):

    def test_case_and_the_root_dot_are_not_differences(self):
        self.assertEqual(tls_names.normalise("Example.COM."), "example.com")

    def test_nothing_normalises_to_nothing(self):
        self.assertEqual(tls_names.normalise(None), "")
        self.assertEqual(tls_names.normalise("   "), "")


class TestMatchingOneName(BaseCase):

    def test_an_exact_name_matches_itself(self):
        self.assertTrue(tls_names.name_matches("a.example.com",
                                               "a.example.com"))

    def test_a_different_name_does_not(self):
        self.assertFalse(tls_names.name_matches("b.example.com",
                                                "a.example.com"))

    def test_a_wildcard_covers_one_label(self):
        self.assertTrue(tls_names.name_matches("a.example.com",
                                               "*.example.com"))

    def test_a_wildcard_does_not_cover_the_bare_domain(self):
        """``*.example.com`` is not ``example.com``, and clients agree."""
        self.assertFalse(tls_names.name_matches("example.com",
                                                "*.example.com"))

    def test_a_wildcard_does_not_cover_two_labels(self):
        self.assertFalse(tls_names.name_matches("a.b.example.com",
                                                "*.example.com"))

    def test_a_partial_wildcard_is_not_honoured(self):
        """Clients refuse it, so honouring it here would disagree with
        the browser the certificate was obtained for."""
        self.assertFalse(tls_names.name_matches("foo.example.com",
                                                "f*.example.com"))

    def test_a_bare_wildcard_matches_nothing(self):
        self.assertFalse(tls_names.name_matches("example.com", "*."))

    def test_empty_input_matches_nothing(self):
        self.assertFalse(tls_names.name_matches("", "*.example.com"))
        self.assertFalse(tls_names.name_matches("a.example.com", ""))


class TestReadingACertificate(BaseCase):

    def test_it_reads_the_alternative_names(self):
        cert, _key = make_pair(["example.test", "*.example.test"])
        self.assertEqual(
            sorted(tls_names.certificate_names(cert)),
            ["*.example.test", "example.test"],
        )

    def test_a_certificate_without_the_extension_covers_nothing(self):
        """The common name is not read, because clients stopped reading
        it — honouring it here would accept what a browser rejects."""
        cert, _key = make_pair(["example.test"], with_san=False)
        self.assertEqual(tls_names.certificate_names(cert), [])

    def test_text_that_is_not_a_certificate_covers_nothing(self):
        self.assertEqual(tls_names.certificate_names("nonsense"), [])
        self.assertEqual(tls_names.certificate_names(""), [])

    def test_covers_asks_every_name(self):
        cert, _key = make_pair(["one.test", "*.two.test"])
        names = tls_names.certificate_names(cert)
        self.assertTrue(tls_names.covers("one.test", names))
        self.assertTrue(tls_names.covers("a.two.test", names))
        self.assertFalse(tls_names.covers("three.test", names))


class TestTheHostDecides(TransactionCase):

    def setUp(self):
        super().setUp()
        self.cert, self.key = make_pair(["example.test", "*.example.test"])
        self.host = self.env["cloud.host"].create({
            "name": "tls-mode-host",
            "ip_address": "198.51.100.40",
            "user": "root",
            "wildcard_domain": "example.test",
        })

    def _hold_the_certificate(self):
        self.host.write({
            "tls_default_cert": self.cert,
            "tls_default_key": self.key,
        })

    def test_a_host_holding_nothing_covers_nothing(self):
        self.assertFalse(self.host._tls_default_covers("a.example.test"))

    def test_half_a_pair_covers_nothing(self):
        """A certificate with no key cannot be served, so it covers no
        name however good its extension looks."""
        self.host.tls_default_cert = self.cert
        self.assertFalse(self.host._tls_default_covers("a.example.test"))

    def test_it_covers_what_the_certificate_says(self):
        self._hold_the_certificate()
        self.assertTrue(self.host._tls_default_covers("a.example.test"))
        self.assertTrue(self.host._tls_default_covers("example.test"))
        self.assertFalse(self.host._tls_default_covers("shop.acme.com"))

    def test_reached_directly_by_default(self):
        self.assertFalse(self.host._name_is_proxied("a.example.test"))

    def test_the_flag_is_the_whole_answer_here(self):
        self.host.behind_cdn = True
        self.assertTrue(self.host._name_is_proxied("a.example.test"))

    def test_a_name_reached_directly_keeps_asking_a_ca(self):
        """Serving the held certificate here would put a warning page in
        front of every visitor: no browser trusts it."""
        self._hold_the_certificate()
        self.assertEqual(
            self.host._router_tls_mode("a.example.test"), "acme",
        )

    def test_a_proxied_name_the_certificate_covers_serves_it(self):
        self._hold_the_certificate()
        self.host.behind_cdn = True
        self.assertEqual(
            self.host._router_tls_mode("a.example.test"), "default",
        )

    def test_a_proxied_name_the_certificate_misses_still_asks(self):
        """A customer's own domain on a host behind our CDN. Neither
        answer works, and this one leaves the failure where the logs
        already look instead of serving a certificate for another name.
        """
        self._hold_the_certificate()
        self.host.behind_cdn = True
        self.assertEqual(
            self.host._router_tls_mode("shop.acme.com"), "acme",
        )

    def test_a_proxied_host_holding_nothing_still_asks(self):
        self.host.behind_cdn = True
        self.assertEqual(
            self.host._router_tls_mode("a.example.test"), "acme",
        )
