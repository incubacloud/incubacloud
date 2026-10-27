"""The issuer's keys: cached, refreshed on rotation, never on demand."""

import base64
import json
import time
import uuid
from unittest.mock import patch

import requests
from joserfc.jwk import RSAKey

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.controllers import _fetch_json, _jwks_cache

from .common import http_response

_URI = "https://idp.example.test/oauth/jwks"
_CACHE_LOGGER = "odoo.addons.incubacloud_mcp.controllers._jwks_cache"


class TestJwksCache(BaseCase):
    """The key set is downloaded once, again on a rotation, and not on demand."""

    def setUp(self):
        """Start from an empty cache, with two keys of the test issuer."""
        super().setUp()
        _jwks_cache.invalidate()
        self.addCleanup(_jwks_cache.invalidate)
        self.keys = {
            kid: RSAKey.generate_key(2048, parameters={"kid": kid})
            for kid in ("kid-A", "kid-B")
        }

    def _response(self, kids, size=None, announce=True):
        """A JWKS response holding *kids* (or a body of *size* bytes)."""
        data = {"keys": [self.keys[kid].as_dict(private=False) for kid in kids]}
        body = b"x" * size if size else None
        return http_response(200, data, body=body, announce=announce)

    def _get(self, **kwargs):
        """Patch ``requests.get`` (autospec) with *kwargs*; return the patcher."""
        return patch.object(requests, "get", autospec=True, **kwargs)

    def _age(self, seconds):
        """Make the cached entry and the last download *seconds* older."""
        stamp, keyset, kids = _jwks_cache._CACHE[_URI]
        _jwks_cache._CACHE[_URI] = (stamp - seconds, keyset, kids)
        attempted_at, failed = _jwks_cache._ATTEMPTS[_URI]
        _jwks_cache._ATTEMPTS[_URI] = (attempted_at - seconds, failed)

    def test_a_known_kid_is_served_from_the_cache(self):
        """A second request for a known key costs no download."""
        with self._get(return_value=self._response(["kid-A"])) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        self.assertEqual(get.call_count, 1)

    def test_unknown_kids_within_thirty_seconds_cost_no_download(self):
        """A hundred random kids right after a download cost nothing more."""
        with self._get(return_value=self._response(["kid-A"])) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            for _i in range(100):
                _jwks_cache.get_keyset(_URI, kid=uuid.uuid4().hex)
        self.assertEqual(get.call_count, 1)

    def test_a_rotation_is_seen_once_the_minimum_wait_passed(self):
        """A new kid past the minimum wait downloads the set again."""
        responses = [self._response(["kid-A"]), self._response(["kid-A", "kid-B"])]
        with self._get(side_effect=responses) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            self._age(_jwks_cache._MIN_REFRESH_SECONDS + 1)
            keyset = _jwks_cache.get_keyset(_URI, kid="kid-B")
        self.assertEqual(get.call_count, 2)
        self.assertIn("kid-B", _jwks_cache._CACHE[_URI][2])
        self.assertIsNotNone(keyset)

    def test_an_expired_entry_is_fetched_again(self):
        """Past its TTL the set is downloaded again, even for a known kid."""
        with self._get(return_value=self._response(["kid-A"])) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
            self._age(_jwks_cache._TTL_SECONDS + 1)
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        self.assertEqual(get.call_count, 2)

    def test_the_download_streams_and_follows_no_redirect(self):
        """The set is read as a stream, and a redirect is not followed."""
        with self._get(return_value=self._response(["kid-A"])) as get:
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)
        self.assertIs(get.call_args.kwargs["stream"], True)

    def test_an_oversized_document_is_refused_and_not_cached(self):
        """Too big, announced or not, is a ValueError and nothing is kept."""
        size = _jwks_cache._MAX_JWKS_BYTES + 1
        for announce in (True, False):
            response = self._response(["kid-A"], size=size, announce=announce)
            with (
                self.subTest(announce=announce),
                self._get(return_value=response),
                self.assertRaises(ValueError),
            ):
                _jwks_cache.get_keyset(_URI, kid="kid-A")
            self.assertNotIn(_URI, _jwks_cache._CACHE)
            if announce:
                # Announced too big: the body is not even read.
                response.iter_content.assert_not_called()
            _jwks_cache.invalidate()

    def test_a_slow_document_is_cut_at_its_deadline(self):
        """A server that drips its answer is cut at the download's deadline."""

        def drip(chunk_size):
            """Yield a byte at a time, slowly."""
            for _i in range(20):
                time.sleep(0.02)
                yield b" "

        response = self._response(["kid-A"], announce=False)
        response.iter_content.side_effect = drip
        with (
            patch.object(_fetch_json, "DEADLINE_SECONDS", 0.05),
            self._get(return_value=response),
            self.assertRaises(ValueError),
        ):
            _jwks_cache.get_keyset(_URI, kid="kid-A")

    def test_a_document_that_is_not_a_key_set_is_a_value_error(self):
        """A list, keys that are not objects or a broken key: ValueError (503)."""
        bodies = (
            [],
            {"keys": "x"},
            {"keys": ["x"]},
            {"keys": [{"kty": "RSA", "kid": "k"}]},
            {"keys": [{"kty": "RSA", "kid": "k", "n": "!!", "e": "AQAB"}]},
        )
        for body in bodies:
            with (
                self.subTest(body=body),
                self._get(return_value=http_response(200, body)),
                self.assertRaises(ValueError),
            ):
                _jwks_cache.get_keyset(_URI, kid="k")
            _jwks_cache.invalidate()

    def test_a_key_set_nested_too_deep_is_a_value_error(self):
        """JSON nested past the parser is a ValueError (503), not a crash."""
        nested = http_response(200, body=b"[" * 50_000)
        with self._get(return_value=nested), self.assertRaises(ValueError):
            _jwks_cache.get_keyset(_URI, kid="kid-A")

    def test_an_issuer_down_is_asked_once_per_minimum_wait(self):
        """Down, it is asked once; unknown kids meanwhile cost no download."""
        down = requests.ConnectionError("down")
        unreachable = (requests.RequestException, ValueError)
        with self._get(side_effect=down) as get:
            for kid in ("kid-X", "kid-Y"):
                with self.subTest(kid=kid), self.assertRaises(unreachable):
                    _jwks_cache.get_keyset(_URI, kid=kid)
        self.assertEqual(get.call_count, 1)

    def test_known_keys_outlive_their_ttl_while_the_issuer_is_down(self):
        """Expired but known, the keys still answer; an unknown kid does not."""
        with self._get(return_value=self._response(["kid-A"])):
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        self._age(_jwks_cache._TTL_SECONDS + 1)
        with self._get(side_effect=requests.ConnectionError("down")) as get:
            with self.assertLogs(_CACHE_LOGGER, level="WARNING"):
                self.assertIsNotNone(_jwks_cache.get_keyset(_URI, kid="kid-A"))
            with self.assertRaises(ValueError):
                _jwks_cache.get_keyset(_URI, kid="kid-B")
        self.assertEqual(get.call_count, 1)

    def test_known_keys_answer_a_day_at_most_after_the_last_good_download(self):
        """Down for a day, the issuer's known keys answer no more (503)."""
        with self._get(return_value=self._response(["kid-A"])):
            _jwks_cache.get_keyset(_URI, kid="kid-A")
        unreachable = (requests.RequestException, ValueError)
        with self._get(side_effect=requests.ConnectionError("down")) as get:
            self._age(_jwks_cache._MAX_STALE_SECONDS - 60)
            with self.assertLogs(_CACHE_LOGGER, level="WARNING"):
                self.assertIsNotNone(_jwks_cache.get_keyset(_URI, kid="kid-A"))
            self._age(120)
            # The download that fails, then the minimum wait after it.
            for _i in range(2):
                with self.assertRaises(unreachable):
                    _jwks_cache.get_keyset(_URI, kid="kid-A")
        self.assertEqual(get.call_count, 2)

    def test_the_kid_is_read_from_the_header_only(self):
        """The kid comes from the first segment; garbage gives None."""
        # {"alg":"RS256","kid":"kid-A"}
        token = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImtpZC1BIn0.e30.sig"
        self.assertEqual(_jwks_cache.unverified_kid(token), "kid-A")
        for bad in ("", "garbage", "e30.e30.x", "!!!.e30.x"):
            with self.subTest(bad=bad):
                self.assertIsNone(_jwks_cache.unverified_kid(bad))

    def test_an_oversized_or_deeply_nested_header_names_no_kid(self):
        """A header past 2 KiB, or nested past the parser's depth, gives None."""
        nested = base64.urlsafe_b64encode(b"[" * 50_000).rstrip(b"=").decode()
        self.assertIsNone(_jwks_cache.unverified_kid(nested + ".e30.x"))
        long_header = base64.urlsafe_b64encode(
            json.dumps({"kid": "k" * 3000}).encode()
        ).decode()
        self.assertIsNone(_jwks_cache.unverified_kid(long_header + ".e30.x"))
        # Past the size cap, the parser's own depth limit is caught too.
        with patch.object(_jwks_cache, "_MAX_HEADER_CHARS", 10**6):
            self.assertIsNone(_jwks_cache.unverified_kid(nested + ".e30.x"))
