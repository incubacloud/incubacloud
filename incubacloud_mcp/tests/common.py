"""Shared fixtures of the MCP tests: an issuer of their own, and users."""

import json
import time
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from joserfc import jws
from joserfc.jwk import RSAKey

from odoo import fields
from odoo.tests.common import new_test_user

from odoo.addons.incubacloud_mcp.controllers import _issuer_metadata, _jwks_cache
from odoo.addons.incubacloud_mcp.models.cloud_settings import RESOURCE_PARAM

ISSUER = "https://idp.example.test"
JWKS_URI = ISSUER + "/oauth/jwks"
CLIENT_ID = "https://client.example.test/oauth/client.json"


def http_response(status, data):
    """A ``requests`` response with *status* and the JSON *data*."""
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.content = json.dumps(data).encode()
    response.json.return_value = data
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(str(status))
    else:
        response.raise_for_status.return_value = None
    return response


class JwtMixin:
    """An issuer of the test's own: its key, its documents, its tokens.

    Mixed into a test case; call :meth:`_setup_issuer` from ``setUp``.
    """

    def _setup_issuer(self, resource):
        """Serve the issuer's documents and sign tokens for *resource*.

        :param str resource: the audience the tokens carry
        """
        self._resource = resource
        self._kid = "kid-" + uuid.uuid4().hex[:8]
        self._rsa_key = RSAKey.generate_key(2048, parameters={"kid": self._kid})
        self._issuer_down = False
        self._jwks_calls = 0
        patcher = patch.object(requests, "get", side_effect=self._issuer_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        for cache in (_jwks_cache, _issuer_metadata):
            cache.invalidate()
            self.addCleanup(cache.invalidate)

    def _issuer_get(self, url, **_kwargs):
        """Answer ``requests.get`` as the test issuer would."""
        if self._issuer_down:
            raise requests.ConnectionError("issuer down")
        if url == ISSUER + "/.well-known/oauth-authorization-server":
            return http_response(200, {"issuer": ISSUER, "jwks_uri": JWKS_URI})
        if url == JWKS_URI:
            self._jwks_calls += 1
            return http_response(200, {"keys": [self._rsa_key.as_dict(private=False)]})
        return http_response(404, {})

    def _token(self, header=None, key=None, **claims):
        """Return an access token; ``None`` in *claims* or *header* drops the entry.

        :param dict header: overrides of the protected header
        :param key: sign with this key instead of the issuer's
        """
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "sub": "4242",
            "aud": self._resource,
            "client_id": CLIENT_ID,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "jti": uuid.uuid4().hex,
            "scope": "openid",
        }
        payload.update(claims)
        protected = {"alg": "RS256", "kid": self._kid, "typ": "at+jwt"}
        protected.update(header or {})
        return jws.serialize_compact(
            {k: v for k, v in protected.items() if v is not None},
            json.dumps({k: v for k, v in payload.items() if v is not None}).encode(),
            key or self._rsa_key,
        )


class McpUsersMixin:
    """Users, API keys and the issuer's configuration."""

    def _cloud_user(self, login, group="group_cloud_developer", extra=""):
        """Create an internal user with the cloud role *group*."""
        groups = f"base.group_user,incubacloud.{group}" + (f",{extra}" if extra else "")
        return new_test_user(self.env, login=login, groups=groups)

    def _api_key(self, user, name=None):
        """Generate an API key of *user* valid for a day; return its value."""
        return (
            self.env["res.users.apikeys"]
            .with_user(user)
            .sudo()
            ._generate(
                "rpc",
                name or f"key-{user.login}",
                fields.Datetime.now() + timedelta(days=1),
            )
        )

    def _configure_issuer(self, resource):
        """Point the panel at the test issuer, with its provider."""
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = ISSUER
        self.env["ir.config_parameter"].sudo().set_param(RESOURCE_PARAM, resource)
        return (
            self.env["auth.oauth.provider"]
            .sudo()
            .create(
                {
                    "name": "MCP test IdP",
                    "client_id": "panel",
                    "body": "IdP",
                    "auth_endpoint": ISSUER + "/oauth/authorize",
                    "validation_endpoint": ISSUER + "/oauth/userinfo",
                    "issuer": ISSUER,
                }
            )
        )
