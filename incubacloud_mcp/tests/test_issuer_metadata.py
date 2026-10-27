"""Where an issuer publishes its keys, as its metadata document says."""

import json
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.controllers import _issuer_metadata

ISSUER = "https://idp.example.test"
OAUTH_DOC = ISSUER + "/.well-known/oauth-authorization-server"
OIDC_DOC = ISSUER + "/.well-known/openid-configuration"


def _response(status, data):
    """A response with *status* and the JSON *data*."""
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.content = json.dumps(data).encode()
    response.json.return_value = data
    return response


class TestIssuerMetadata(BaseCase):
    def setUp(self):
        super().setUp()
        _issuer_metadata.invalidate()
        self.addCleanup(_issuer_metadata.invalidate)

    def test_rfc_8414_first_with_the_path_after_the_well_known(self):
        self.assertEqual(
            _issuer_metadata.metadata_urls("https://idp.example.test/tenant1"),
            [
                "https://idp.example.test/.well-known/oauth-authorization-server/tenant1",
                "https://idp.example.test/tenant1/.well-known/openid-configuration",
            ],
        )

    def test_the_oauth_document_answers_and_is_cached_an_hour(self):
        doc = {"issuer": ISSUER, "jwks_uri": ISSUER + "/oauth/jwks"}
        with patch.object(requests, "get", return_value=_response(200, doc)) as get:
            self.assertEqual(
                _issuer_metadata.jwks_uri_for(ISSUER), ISSUER + "/oauth/jwks"
            )
            self.assertEqual(
                _issuer_metadata.jwks_uri_for(ISSUER), ISSUER + "/oauth/jwks"
            )
        self.assertEqual([call.args[0] for call in get.call_args_list], [OAUTH_DOC])

    def test_openid_configuration_when_the_oauth_document_is_missing(self):
        doc = {"issuer": ISSUER, "jwks_uri": ISSUER + "/oauth/jwks"}
        responses = [_response(404, {}), _response(200, doc)]
        with patch.object(requests, "get", side_effect=responses) as get:
            self.assertEqual(
                _issuer_metadata.jwks_uri_for(ISSUER), ISSUER + "/oauth/jwks"
            )
        self.assertEqual(
            [call.args[0] for call in get.call_args_list], [OAUTH_DOC, OIDC_DOC]
        )

    def test_a_document_of_another_issuer_is_refused(self):
        doc = {
            "issuer": "https://evil.example.test",
            "jwks_uri": "https://evil.example.test/jwks",
        }
        with (
            patch.object(requests, "get", return_value=_response(200, doc)),
            self.assertRaises(ValueError),
        ):
            _issuer_metadata.jwks_uri_for(ISSUER)

    def test_an_unreachable_issuer_raises(self):
        with (
            patch.object(requests, "get", side_effect=requests.ConnectionError("down")),
            self.assertRaises(requests.RequestException),
        ):
            _issuer_metadata.jwks_uri_for(ISSUER)

    def test_after_an_hour_it_is_asked_again(self):
        doc = {"issuer": ISSUER, "jwks_uri": ISSUER + "/oauth/jwks"}
        with patch.object(requests, "get", return_value=_response(200, doc)) as get:
            _issuer_metadata.jwks_uri_for(ISSUER)
            stamp, uri = _issuer_metadata._CACHE[ISSUER]
            _issuer_metadata._CACHE[ISSUER] = (
                stamp - _issuer_metadata._TTL_SECONDS - 1,
                uri,
            )
            _issuer_metadata.jwks_uri_for(ISSUER)
        self.assertEqual(get.call_count, 2)
