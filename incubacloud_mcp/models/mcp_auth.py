"""Who an agent's credential belongs to.

Two credentials, both in ``Authorization: Bearer``: an OAuth access token
of the configured issuer (RS256, RFC 9068 ``at+jwt``, audience this
panel's ``/mcp``), or one of Odoo's own API keys where this panel allows
them. Either resolves to an internal user with a cloud role, and the
request then runs as that user — never as the superuser.
"""

import ipaddress
import json
import logging
import re

import requests
from joserfc import jws, jwt
from joserfc.errors import JoseError

from odoo import SUPERUSER_ID, api, models, tools

from ..controllers._issuer_metadata import issuer_spellings, jwks_uri_for
from ..controllers._jwks_cache import get_keyset, unverified_kid
from ..protocol.canonical import resource_metadata_url
from ..protocol.context import Principal, Reply
from ..protocol.errors import AuthError

_logger = logging.getLogger(__name__)

#: Clock skew tolerated on ``exp``, ``nbf`` and ``iat``.
_LEEWAY_SECONDS = 60
_ACCESS_TOKEN_TYPES = ("at+jwt", "application/at+jwt")
#: A compact JWS: three base64url segments.
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
#: Failed authentications per address and minute before it is refused.
AUTH_FAILURES_PER_MIN = 60
#: No credential is longer: a JWT of this panel's issuer is about 1 KB.
_MAX_CREDENTIAL_CHARS = 8 * 1024
#: Failures from IPv6 addresses are counted per network of this size: one
#: host usually holds the whole /64.
_IPV6_FAILURE_PREFIX = 64
#: Odoo's API keys are looked up by their first characters (``index``).
_APIKEY_INDEX_SIZE = 8
_ERROR_CODES = {401: "invalid_token", 403: "forbidden", 503: "temporarily_unavailable"}


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
        # The registry compares a list by membership; an issuer or a
        # subject is one string (aud alone may list several audiences).
        if not all(isinstance(claims.get(name), str) for name in ("iss", "sub")):
            raise self._invalid_token()
        registry = jwt.JWTClaimsRegistry(
            leeway=_LEEWAY_SECONDS,
            iss={"essential": True, "values": list(issuer_spellings(issuer))},
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

    @api.model
    def _authenticate(self, authorization):
        """Return the :class:`Principal` of an ``Authorization`` header.

        :param authorization: the header's value, or None
        :raises AuthError: 401, 403 or 503 (see the module docstring)
        """
        scheme, _sep, credential = (authorization or "").partition(" ")
        credential = credential.strip()
        if scheme.lower() != "bearer" or not credential:
            raise AuthError(401, self.env._("Authentication required."))
        if len(credential) > _MAX_CREDENTIAL_CHARS:
            raise self._invalid_token()
        Settings = self.env["cloud.settings"].sudo()
        if _JWT_RE.match(credential):
            issuer = Settings._mcp_issuer()
            if not issuer:
                raise self._invalid_token()
            claims = self._verify_access_token(
                credential, issuer, Settings._mcp_resource()
            )
            user = self._user_for_claims(issuer, claims)
            label = str(claims["client_id"])
        else:
            if not Settings._mcp_api_keys_allowed():
                raise AuthError(
                    401,
                    self.env._("On this panel agents connect with OAuth."),
                    token_presented=True,
                )
            user, key_name = self._user_for_api_key(credential)
            label = f"API key {key_name}"
        self._check_user(user)
        return Principal(uid=user.id, credential=label)

    @api.model
    def _user_for_claims(self, issuer, claims):
        """Return the user the token's ``sub`` names through its provider.

        Every enabled provider with *issuer* counts; a disabled one lets no
        agent in, as it lets no one sign in.

        :raises AuthError: 503 when no enabled provider has *issuer*
            (configuration), 403 when the user is not linked yet
        """
        providers = (
            self.env["auth.oauth.provider"]
            .sudo()
            .search(
                [
                    ("issuer", "in", issuer_spellings(issuer)),
                    ("enabled", "=", True),
                ]
            )
        )
        if not providers:
            _logger.error(
                "[mcp] no enabled auth.oauth.provider has issuer %s: agents' "
                "tokens cannot be matched to users",
                issuer,
            )
            raise AuthError(
                503,
                self.env._("This panel's sign-in is not completely configured."),
                retry_after=300,
            )
        user = (
            self.env["res.users"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("oauth_provider_id", "in", providers.ids),
                    ("oauth_uid", "=", str(claims["sub"])),
                ],
                limit=1,
            )
        )
        if not user:
            raise AuthError(
                403,
                self.env._(
                    "Sign in to the panel once to create your user, "
                    "then connect the agent again."
                ),
            )
        return user

    @api.model
    def _user_for_api_key(self, key):
        """Return ``(user, key name)`` for one of Odoo's API keys.

        :raises AuthError: 401 when the key is unknown, expired or revoked
        """
        uid = (
            self.env["res.users.apikeys"]
            .sudo()
            ._check_credentials(scope="rpc", key=key)
        )
        if not uid:
            raise AuthError(
                401,
                self.env._("The API key is not valid, has expired or was revoked."),
                token_presented=True,
            )
        self.env.cr.execute(
            "SELECT name FROM res_users_apikeys WHERE user_id = %s AND index = %s "
            "ORDER BY id DESC LIMIT 1",
            (uid, key[:_APIKEY_INDEX_SIZE]),
        )
        row = self.env.cr.fetchone()
        return self.env["res.users"].sudo().browse(uid), (row[0] if row else "?")

    @api.model
    def _check_user(self, user):
        """Refuse a user who cannot use the panel (403, never 401).

        A 401 here would send the client through OAuth again, forever.
        """
        Auth = self.with_context(lang=user.lang or self.env.lang)
        if user.id == SUPERUSER_ID:
            # Never the superuser, whatever its groups: it bypasses the
            # record rules every route relies on.
            raise AuthError(
                403, Auth.env._("Agents act for the panel's internal users only.")
            )
        if not user.active:
            raise AuthError(403, Auth.env._("Your user on this panel is inactive."))
        if not user._is_internal():
            raise AuthError(
                403, Auth.env._("Agents act for the panel's internal users only.")
            )
        mixin = self.env["cloud.security.mixin"].with_user(user)
        if not mixin._has_cloud_group("group_cloud_user"):
            raise AuthError(403, Auth.env._("Your user has no role on this panel."))

    @api.model
    def _failure_bucket(self, ip):
        """Return the rate-limit bucket of failed authentications of *ip*.

        An IPv4 address counts alone; an IPv6 one with its /64 (an
        IPv4-mapped one as its IPv4). Anything unparsable counts as is.
        """
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return f"mcp_auth_fail:{ip}"
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address.version == 6:
            network = ipaddress.ip_network(
                f"{address}/{_IPV6_FAILURE_PREFIX}", strict=False
            )
            return f"mcp_auth_fail:{network}"
        return f"mcp_auth_fail:{address}"

    @api.model
    def _auth_blocked(self, ip):
        """Whether *ip* failed too often in the last minute (no counting).

        Bypassed in tests unless ``force_rate_limit`` is in the context,
        like ``cloud.rate.limit.hit``.
        """
        if tools.config.get("test_enable") and not self.env.context.get(
            "force_rate_limit"
        ):
            return False
        self.env.cr.execute(
            "SELECT count FROM cloud_rate_limit WHERE bucket = %s "
            "AND NOW() - window_start < INTERVAL '60 seconds'",
            (self._failure_bucket(ip),),
        )
        row = self.env.cr.fetchone()
        return bool(row and row[0] >= AUTH_FAILURES_PER_MIN)

    @api.model
    def _count_failure(self, ip):
        """Count a failed authentication of *ip*, in a cursor of its own.

        Its own transaction: the request's may roll back, and parallel
        failures must not fight over the bucket's row inside it.
        """
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, dict(self.env.context))
            env["cloud.rate.limit"].hit(
                self._failure_bucket(ip), max_per_window=AUTH_FAILURES_PER_MIN
            )
            cr.commit()

    @api.model
    def _auth_reply(self, error):
        """Return the HTTP answer to an :class:`AuthError`.

        A 401 challenges with ``resource_metadata`` when an issuer is
        configured, with a ``realm`` otherwise (API keys only) — and when
        this panel has no usable ``/mcp`` URL to point at, which is logged.
        """
        headers = {}
        if error.status == 401:
            Settings = self.env["cloud.settings"].sudo()
            issuer, resource = Settings._mcp_issuer(), Settings._mcp_resource()
            if issuer and resource:
                metadata = resource_metadata_url(resource)
                challenge = f'Bearer resource_metadata="{metadata}"'
            else:
                if issuer:
                    _logger.error(
                        "[mcp] agents' issuer %s is set but this panel has no "
                        "usable MCP URL (incubacloud.mcp.resource, web.base.url)",
                        issuer,
                    )
                challenge = 'Bearer realm="incubacloud-panel"'
            if error.token_presented:
                challenge += ', error="invalid_token"'
            headers["WWW-Authenticate"] = challenge
        if error.retry_after:
            headers["Retry-After"] = str(error.retry_after)
        if error.token_presented or error.status != 401:
            code = _ERROR_CODES[error.status]
        else:
            code = "unauthorized"
        return Reply(
            error.status, {"error": code, "error_description": error.message}, headers
        )
