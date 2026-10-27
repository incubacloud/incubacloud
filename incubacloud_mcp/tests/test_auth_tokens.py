"""Only an at+jwt of the configured issuer, for this panel, opens it."""

import time

from joserfc.jwk import RSAKey

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.protocol.errors import AuthError

from .common import ISSUER, JwtMixin

RESOURCE = "https://panel.example.test/mcp"
AUTH_LOGGER = "odoo.addons.incubacloud_mcp.models.mcp_auth"


class TestAccessTokens(JwtMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_issuer(RESOURCE)
        self.auth = self.env["incubacloud.mcp.auth"]

    def _verify(self, token):
        """Validate *token* for the test panel."""
        return self.auth._verify_access_token(token, ISSUER, RESOURCE)

    def _refused(self, token, status=401):
        """Assert *token* is refused with *status*."""
        with self.assertRaises(AuthError) as caught:
            self._verify(token)
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def test_a_good_token_gives_its_claims(self):
        claims = self._verify(self._token())
        self.assertEqual(claims["sub"], "4242")

    def test_bad_tokens_are_refused(self):
        now = int(time.time())
        other_key = RSAKey.generate_key(2048, parameters={"kid": self._kid})
        cases = {
            "signed by another key": self._token(key=other_key),
            "another issuer": self._token(iss="https://other.example.test"),
            "another panel": self._token(aud="https://other-panel.example.test/mcp"),
            "expired": self._token(iat=now - 900, nbf=now - 900, exp=now - 300),
            "not yet valid": self._token(nbf=now + 300),
            "an id_token": self._token(header={"typ": "JWT"}),
            "no typ": self._token(header={"typ": None}),
            "no client_id": self._token(client_id=None),
            "no jti": self._token(jti=None),
            "unknown key": self._token(header={"kid": "nobody"}),
        }
        for label, token in cases.items():
            with self.subTest(label):
                self._refused(token)
        self._refused("a.b.c")

    def test_a_minute_of_clock_skew_is_tolerated(self):
        now = int(time.time())
        self._verify(self._token(iat=now - 600, nbf=now - 600, exp=now - 30))

    def test_an_unreachable_issuer_is_503_with_retry_after(self):
        self._issuer_down = True
        with self.assertLogs(AUTH_LOGGER, level="WARNING"):
            error = self._refused(self._token(), status=503)
        self.assertTrue(error.retry_after)
