"""Where an issuer publishes its keys, as its metadata document says."""

from unittest.mock import patch

import requests

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.controllers import _issuer_metadata

from .common import http_response

ISSUER = "https://idp.example.test"
OAUTH_DOC = ISSUER + "/.well-known/oauth-authorization-server"
OIDC_DOC = ISSUER + "/.well-known/openid-configuration"
DOC = {"issuer": ISSUER, "jwks_uri": ISSUER + "/oauth/jwks"}
_METADATA_LOGGER = "odoo.addons.incubacloud_mcp.controllers._issuer_metadata"


class TestIssuerMetadata(BaseCase):
    """The jwks_uri comes from the issuer's own document, cached an hour."""

    def setUp(self):
        """Start from an empty cache."""
        super().setUp()
        _issuer_metadata.invalidate()
        self.addCleanup(_issuer_metadata.invalidate)

    def _get(self, **kwargs):
        """Patch ``requests.get`` (autospec) with *kwargs*; return the patcher."""
        return patch.object(requests, "get", autospec=True, **kwargs)

    def _age(self, seconds):
        """Make the cached document and the last failure *seconds* older."""
        for store in (_issuer_metadata._CACHE, _issuer_metadata._FAILURES):
            if ISSUER in store:
                stamp, value = store[ISSUER]
                store[ISSUER] = (stamp - seconds, value)

    def test_rfc_8414_first_with_the_path_after_the_well_known(self):
        """RFC 8414's document is tried first, the path after the well-known."""
        self.assertEqual(
            _issuer_metadata.metadata_urls("https://idp.example.test/tenant1"),
            [
                "https://idp.example.test/.well-known/oauth-authorization-server/tenant1",
                "https://idp.example.test/tenant1/.well-known/openid-configuration",
            ],
        )

    def test_the_oauth_document_answers_and_is_cached_an_hour(self):
        """One download serves the next calls, streamed and without redirects."""
        with self._get(return_value=http_response(200, DOC)) as get:
            self.assertEqual(_issuer_metadata.jwks_uri_for(ISSUER), DOC["jwks_uri"])
            self.assertEqual(_issuer_metadata.jwks_uri_for(ISSUER), DOC["jwks_uri"])
        self.assertEqual([call.args[0] for call in get.call_args_list], [OAUTH_DOC])
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)
        self.assertIs(get.call_args.kwargs["stream"], True)

    def test_openid_configuration_when_the_oauth_document_is_missing(self):
        """A 404 on RFC 8414's document falls back to OpenID Connect's."""
        responses = [http_response(404, {}), http_response(200, DOC)]
        with self._get(side_effect=responses) as get:
            self.assertEqual(_issuer_metadata.jwks_uri_for(ISSUER), DOC["jwks_uri"])
        self.assertEqual(
            [call.args[0] for call in get.call_args_list], [OAUTH_DOC, OIDC_DOC]
        )

    def test_an_issuer_that_ends_in_a_slash_is_the_configured_one(self):
        """Auth0's issuer ends in a slash; the setting does not."""
        doc = {"issuer": ISSUER + "/", "jwks_uri": DOC["jwks_uri"]}
        with self._get(return_value=http_response(200, doc)):
            self.assertEqual(_issuer_metadata.jwks_uri_for(ISSUER), DOC["jwks_uri"])

    def test_a_document_of_another_issuer_is_refused(self):
        """A document naming another issuer gives no jwks_uri."""
        doc = {
            "issuer": "https://evil.example.test",
            "jwks_uri": "https://evil.example.test/jwks",
        }
        with (
            self._get(return_value=http_response(200, doc)),
            self.assertRaises(ValueError),
        ):
            _issuer_metadata.jwks_uri_for(ISSUER)

    def test_a_jwks_uri_that_is_not_https_is_refused(self):
        """Keys fetched over plain http could be anyone's."""
        doc = {"issuer": ISSUER, "jwks_uri": "http://idp.example.test/oauth/jwks"}
        with (
            self._get(return_value=http_response(200, doc)),
            self.assertRaises(ValueError),
        ):
            _issuer_metadata.jwks_uri_for(ISSUER)

    def test_an_oversized_document_is_refused(self):
        """Past 64 KB, announced or not, a document is a ValueError."""
        body = b"x" * (_issuer_metadata._MAX_BYTES + 1)
        for announce in (True, False):
            with (
                self.subTest(announce=announce),
                self._get(
                    return_value=http_response(200, body=body, announce=announce)
                ),
                self.assertRaises(ValueError),
            ):
                _issuer_metadata.jwks_uri_for(ISSUER)
            _issuer_metadata.invalidate()

    def test_a_document_nested_too_deep_is_a_failure_it_remembers(self):
        """JSON nested past the parser is a ValueError, remembered as a failure."""
        nested = http_response(200, body=b"[" * 50_000)
        with self._get(return_value=nested), self.assertRaises(ValueError):
            _issuer_metadata.jwks_uri_for(ISSUER)
        self.assertIn(ISSUER, _issuer_metadata._FAILURES)

    def test_an_unreachable_issuer_raises(self):
        """Down, the issuer's error comes out (the caller answers 503)."""
        with (
            self._get(side_effect=requests.ConnectionError("down")),
            self.assertRaises(requests.RequestException),
        ):
            _issuer_metadata.jwks_uri_for(ISSUER)

    def test_an_issuer_down_is_asked_again_only_after_the_retry_wait(self):
        """Ten calls while it is down cost one round of downloads."""
        unreachable = (requests.RequestException, ValueError)
        with self._get(side_effect=requests.ConnectionError("down")) as get:
            for _i in range(10):
                with self.assertRaises(unreachable):
                    _issuer_metadata.jwks_uri_for(ISSUER)
            self.assertEqual(get.call_count, 2)
            self._age(_issuer_metadata._RETRY_SECONDS + 1)
            with self.assertRaises(unreachable):
                _issuer_metadata.jwks_uri_for(ISSUER)
        self.assertEqual(get.call_count, 4)

    def test_a_known_jwks_uri_outlives_its_hour_while_the_issuer_is_down(self):
        """Past the hour, a down issuer leaves the known address in use."""
        with self._get(return_value=http_response(200, DOC)):
            _issuer_metadata.jwks_uri_for(ISSUER)
        self._age(_issuer_metadata._TTL_SECONDS + 1)
        with (
            self._get(side_effect=requests.ConnectionError("down")),
            self.assertLogs(_METADATA_LOGGER, level="WARNING"),
        ):
            self.assertEqual(_issuer_metadata.jwks_uri_for(ISSUER), DOC["jwks_uri"])

    def test_after_an_hour_it_is_asked_again(self):
        """Past the hour, the document is downloaded again."""
        with self._get(return_value=http_response(200, DOC)) as get:
            _issuer_metadata.jwks_uri_for(ISSUER)
            self._age(_issuer_metadata._TTL_SECONDS + 1)
            _issuer_metadata.jwks_uri_for(ISSUER)
        self.assertEqual(get.call_count, 2)
