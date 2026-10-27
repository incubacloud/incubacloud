"""Only an at+jwt of the configured issuer, for this panel, opens it."""

import time

from joserfc.jwk import ECKey, RSAKey

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.protocol.errors import AuthError

from .common import ISSUER, JwtMixin

RESOURCE = "https://panel.example.test/mcp"
AUTH_LOGGER = "odoo.addons.incubacloud_mcp.models.mcp_auth"


class TestAccessTokens(JwtMixin, TransactionCase):
    """The access token itself: signature, type, claims and their margins."""

    def setUp(self):
        """Stand up the test issuer for the test panel."""
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
        """A token of the issuer for this panel gives back its claims."""
        claims = self._verify(self._token())
        self.assertEqual(claims["sub"], "4242")

    def test_bad_tokens_are_refused(self):
        """Another key, issuer or panel, out of time, wrong type, incomplete: 401."""
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

    def test_an_iss_that_ends_in_a_slash_is_the_configured_issuer(self):
        """An iss with a trailing slash names the configured issuer."""
        self._issuer_published = ISSUER + "/"
        claims = self._verify(self._token(iss=ISSUER + "/"))
        self.assertEqual(claims["iss"], ISSUER + "/")

    def test_a_token_missing_a_required_claim_is_refused(self):
        """Without exp, iat, sub, aud or iss a token opens nothing."""
        for claim in ("exp", "iat", "sub", "aud", "iss"):
            with self.subTest(claim=claim):
                self._refused(self._token(**{claim: None}))

    def test_the_clock_skew_is_a_minute_and_no_more(self):
        """Expired 90 s ago, or valid only in 90 s: refused."""
        now = int(time.time())
        self._refused(self._token(iat=now - 600, exp=now - 90))
        self._refused(self._token(nbf=now + 90))

    def test_another_algorithm_is_refused_even_with_a_published_key(self):
        """ES256 with a key the issuer publishes: only RS256 opens the panel."""
        ec_key = ECKey.generate_key("P-256", parameters={"kid": "kid-ec"})
        self._extra_keys.append(ec_key)
        self._refused(self._token(header={"alg": "ES256", "kid": "kid-ec"}, key=ec_key))

    def test_iss_and_sub_are_strings_and_aud_may_be_a_list(self):
        """A list where a string belongs is refused; aud may list audiences."""
        self._refused(self._token(iss=[ISSUER]))
        self._refused(self._token(sub=["4242"]))
        claims = self._verify(
            self._token(aud=[RESOURCE, "https://other-panel.example.test/mcp"])
        )
        self.assertEqual(claims["sub"], "4242")

    def test_a_minute_of_clock_skew_is_tolerated(self):
        """Expired 30 s ago is still within the minute of margin."""
        now = int(time.time())
        self._verify(self._token(iat=now - 600, nbf=now - 600, exp=now - 30))

    def test_an_unreachable_issuer_is_503_with_retry_after(self):
        """Keys that cannot be fetched are a 503 with Retry-After, logged."""
        self._issuer_down = True
        with self.assertLogs(AUTH_LOGGER, level="WARNING"):
            error = self._refused(self._token(), status=503)
        self.assertTrue(error.retry_after)
