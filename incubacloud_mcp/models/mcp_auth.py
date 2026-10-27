"""Who an agent's credential belongs to.

Two credentials, both in ``Authorization: Bearer``: an OAuth access token
of the configured issuer (RS256, RFC 9068 ``at+jwt``, audience this
panel's ``/mcp``), or one of Odoo's own API keys where this panel allows
them. Either resolves to an internal user with a cloud role, and the
request then runs as that user — never as the superuser.
"""

import json
import logging

import requests
from joserfc import jws, jwt
from joserfc.errors import JoseError

from odoo import api, models

from ..controllers._issuer_metadata import jwks_uri_for
from ..controllers._jwks_cache import get_keyset, unverified_kid
from ..protocol.errors import AuthError

_logger = logging.getLogger(__name__)

#: Clock skew tolerated on ``exp``, ``nbf`` and ``iat``.
_LEEWAY_SECONDS = 60
_ACCESS_TOKEN_TYPES = ("at+jwt", "application/at+jwt")


class McpAuth(models.AbstractModel):
    _name = "incubacloud.mcp.auth"
    _description = "Credentials of agents on the MCP endpoint"

    @api.model
    def _invalid_token(self):
        """Return the 401 for a credential that does not open the panel."""
        return AuthError(
            401,
            self.env._("The access token is not valid for this panel."),
            token_presented=True,
        )

    @api.model
    def _verify_access_token(self, token, issuer, resource):
        """Return the claims of *token*, or raise.

        :param str token: the compact JWT the agent presented
        :param str issuer: the configured issuer
        :param str resource: this panel's canonical ``/mcp`` URL
        :rtype: dict
        :raises AuthError: 401 when the token is not one of *issuer*'s
            access tokens for *resource*; 503 when the issuer's keys
            cannot be fetched
        """
        try:
            keyset = get_keyset(jwks_uri_for(issuer), kid=unverified_kid(token))
        except (requests.RequestException, ValueError) as exc:
            _logger.warning(
                "[mcp] the issuer %s cannot be asked for its keys: %s", issuer, exc
            )
            raise AuthError(
                503,
                self.env._("The sign-in service cannot be reached. Try again shortly."),
                retry_after=30,
            ) from exc
        try:
            signed = jws.deserialize_compact(token, keyset, algorithms=["RS256"])
            header = signed.headers()
            claims = json.loads(signed.payload)
        except (JoseError, ValueError, TypeError, KeyError) as exc:
            raise self._invalid_token() from exc
        token_type = str(header.get("typ", "")).lower()
        if token_type not in _ACCESS_TOKEN_TYPES or not isinstance(claims, dict):
            raise self._invalid_token()
        registry = jwt.JWTClaimsRegistry(
            leeway=_LEEWAY_SECONDS,
            iss={"essential": True, "value": issuer},
            aud={"essential": True, "value": resource},
            sub={"essential": True},
            exp={"essential": True},
            iat={"essential": True},
            jti={"essential": True},
            client_id={"essential": True},
        )
        try:
            registry.validate(claims)
        except JoseError as exc:
            raise self._invalid_token() from exc
        return claims
