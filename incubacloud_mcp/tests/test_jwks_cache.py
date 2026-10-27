"""The issuer's keys: cached, refreshed on rotation, never on demand."""

import json
import uuid
from unittest.mock import MagicMock, patch

import requests
from joserfc.jwk import RSAKey

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.controllers import _jwks_cache

_URI = "https://idp.example.test/oauth/jwks"


class TestJwksCache(BaseCase):
    def setUp(self):
        super().setUp()
        _jwks_cache.invalidate()
        self.addCleanup(_jwks_cache.invalidate)
        self.keys = {
            kid: RSAKey.generate_key(2048, parameters={"kid": kid})
            for kid in ("kid-A", "kid-B")
        }

    def _response(self, kids, size=None):
        """A JWKS response holding *kids* (or a body of *size* bytes)."""
        data = {"keys": [self.keys[kid].as_dict(private=False) for kid in kids]}
        response = MagicMock(spec=requests.Response)
        response.content = b"x" * size if size else json.dumps(data).encode()
        response.json.return_value = data
        response.raise_for_status.return_value = None
        return response

    def _age(self, seconds):
        """Make the cached entry *seconds* older."""
        stamp, keyset, kids = _jwks_cache._CACHE[_URI]
        _jwks_cache._CACHE[_URI] = (stamp - seconds, keyset, kids)

    def test_a_known_kid_is_served_from_the_cache(self):
        with patch.object(
            requests, "get", return_value=self._response(["kid-A"])
        ) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        self.assertEqual(get.call_count, 1)

    def test_unknown_kids_within_thirty_seconds_cost_no_download(self):
        with patch.object(
            requests, "get", return_value=self._response(["kid-A"])
        ) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            for _i in range(100):
                _jwks_cache.get_keyset(_URI, kid=uuid.uuid4().hex)
        self.assertEqual(get.call_count, 1)

    def test_a_rotation_is_seen_once_the_minimum_wait_passed(self):
        responses = [self._response(["kid-A"]), self._response(["kid-A", "kid-B"])]
        with patch.object(requests, "get", side_effect=responses) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            self._age(_jwks_cache._MIN_REFRESH_SECONDS + 1)
            keyset = _jwks_cache.get_keyset(_URI, kid="kid-B")
        self.assertEqual(get.call_count, 2)
        self.assertIn("kid-B", _jwks_cache._CACHE[_URI][2])
        self.assertIsNotNone(keyset)

    def test_an_expired_entry_is_fetched_again(self):
        with patch.object(
            requests, "get", return_value=self._response(["kid-A"])
        ) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            self._age(_jwks_cache._TTL_SECONDS + 1)
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        self.assertEqual(get.call_count, 2)

    def test_an_oversized_document_is_refused_and_not_cached(self):
        response = self._response(["kid-A"], size=_jwks_cache._MAX_JWKS_BYTES + 1)
        with (
            patch.object(requests, "get", return_value=response),
            self.assertRaises(ValueError),
        ):
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        self.assertNotIn(_URI, _jwks_cache._CACHE)

    def test_the_kid_is_read_from_the_header_only(self):
        # {"alg":"RS256","kid":"kid-A"}
        token = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImtpZC1BIn0.e30.sig"
        self.assertEqual(_jwks_cache.unverified_kid(token), "kid-A")
        for bad in ("", "garbage", "e30.e30.x", "!!!.e30.x"):
            with self.subTest(bad=bad):
                self.assertIsNone(_jwks_cache.unverified_kid(bad))
