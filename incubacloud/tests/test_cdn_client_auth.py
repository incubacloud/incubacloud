"""A host behind the CDN can require the CDN's client certificate.

The CDN's addresses are shared by all of its customers, so an allowlist
of them admits anybody's zone pointed at our origin. Requiring the client
certificate our zone presents (Authenticated Origin Pulls) closes that.
Only on a host reached through the CDN and closed to direct traffic:
anywhere else a visitor would be asked for a certificate it does not have.
"""
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


def _self_signed_pem():
    """Return a throwaway self-signed certificate, PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "origin-pull test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )


class TestCdnClientAuth(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ca = _self_signed_pem()

    def _host(self, **vals):
        return self.env["cloud.host"].create({
            "name": "aop-host",
            "ip_address": "192.0.2.70",
            "user": "root",
            "wildcard_domain": "aop.example.com",
        } | vals)

    def test_required_only_behind_the_cdn_with_the_origin_closed(self):
        closed = self._host(
            behind_cdn=True, block_direct_access=True, cdn_client_ca=self.ca,
        )
        doc = closed._shipped_tls_client_auth_yml()
        self.assertIn("clientAuthType: RequireAndVerifyClientCert", doc)
        self.assertIn("- /etc/certs/cdn-client-ca.pem", doc)
        self.assertIn("    default:\n      clientAuth:", doc)
        for flags in (
            {"behind_cdn": False, "block_direct_access": True},
            {"behind_cdn": True, "block_direct_access": False},
        ):
            closed.write(flags)
            self.assertEqual(closed._effective_cdn_client_ca(), "", flags)
            self.assertEqual(closed._shipped_tls_client_auth_yml(), "", flags)

    def test_nothing_without_an_authority(self):
        host = self._host(behind_cdn=True, block_direct_access=True)
        self.assertEqual(host._shipped_tls_client_auth_yml(), "")

    def test_an_unreadable_authority_is_refused(self):
        """The proxy could not complete any handshake with it."""
        with self.assertRaises(ValidationError):
            self._host(cdn_client_ca="not a certificate")
        with self.assertRaises(ValidationError):
            self._host(cdn_client_ca=self.ca + "\n" + self.ca)

    def test_a_private_key_pasted_along_is_refused(self):
        """The parser reads the first certificate and ignores the rest:
        the key would go unnoticed here and land on every host."""
        key = (
            "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
        )
        with self.assertRaises(ValidationError):
            self._host(cdn_client_ca=self.ca + "\n" + key)
        with self.assertRaises(ValidationError):
            self._host(cdn_client_ca=key)

    def test_it_counts_as_proxy_configuration(self):
        """Changing it is a pending change, applied by the proxy push."""
        host = self._host()
        self.assertIn("cdn_client_ca", host._config_snapshot_fields())
