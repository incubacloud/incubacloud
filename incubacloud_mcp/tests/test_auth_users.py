"""A credential becomes a panel user — or a refusal that says why."""

import base64
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.incubacloud_mcp.models.cloud_settings import RESOURCE_PARAM
from odoo.addons.incubacloud_mcp.models.mcp_auth import AUTH_FAILURES_PER_MIN
from odoo.addons.incubacloud_mcp.protocol.errors import AuthError

from .common import CLIENT_ID, ISSUER, JwtMixin, McpUsersMixin

RESOURCE = "https://panel.example.test/mcp"
AUTH_LOGGER = "odoo.addons.incubacloud_mcp.models.mcp_auth"


@tagged("post_install", "-at_install")
class TestAuthenticate(JwtMixin, McpUsersMixin, TransactionCase):
    """``post_install``: its users need a partner, and a module installed
    later may add a column to it."""

    def setUp(self):
        """Configure the issuer and link a developer to the token's sub."""
        super().setUp()
        self._setup_issuer(RESOURCE)
        self.provider = self._configure_issuer(RESOURCE)
        self.user = self._cloud_user("mcp-auth-dev")
        self.user.write({"oauth_provider_id": self.provider.id, "oauth_uid": "4242"})
        self.auth = self.env["incubacloud.mcp.auth"]

    def _refused(self, authorization, status):
        """Assert *authorization* is refused with *status*; return the error."""
        with self.assertRaises(AuthError) as caught:
            self.auth._authenticate(authorization)
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def test_no_credential_is_401(self):
        """No bearer credential is a 401 that says no token was presented."""
        for header in (None, "", "Basic abc", "Bearer "):
            with self.subTest(header=header):
                self.assertFalse(self._refused(header, 401).token_presented)

    def test_a_token_resolves_to_its_user(self):
        """A token gives its user, and its client_id as the credential."""
        principal = self.auth._authenticate(f"Bearer {self._token()}")
        self.assertEqual(
            (principal.uid, principal.credential), (self.user.id, CLIENT_ID)
        )

    def test_an_issuer_that_ends_in_a_slash_like_auth0s_works(self):
        """An issuer configured and published with a slash still finds the user."""
        self._issuer_published = ISSUER + "/"
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = ISSUER + "/"
        self.provider.issuer = ISSUER + "/"
        principal = self.auth._authenticate(f"Bearer {self._token(iss=ISSUER + '/')}")
        self.assertEqual(principal.uid, self.user.id)

    def test_a_token_without_a_local_user_says_sign_in_once(self):
        """A good token without a local user is a 403 that says what to do."""
        error = self._refused(f"Bearer {self._token(sub='9999')}", 403)
        self.assertIn("Sign in to the panel once", error.message)

    def test_an_inactive_user_or_one_without_role_is_403(self):
        """An inactive user, or one without a cloud role, is a 403 that says why."""
        self.user.active = False
        error = self._refused(f"Bearer {self._token()}", 403)
        self.assertIn("inactive", error.message)
        self.user.active = True
        developer = self.env.ref("incubacloud.group_cloud_developer")
        self.user.group_ids = [(3, developer.id)]
        error = self._refused(f"Bearer {self._token()}", 403)
        self.assertIn("no role", error.message)

    def test_a_user_of_another_provider_with_the_same_uid_is_403(self):
        """The uid counts only together with the issuer's provider."""
        other = self.env["auth.oauth.provider"].create(
            {
                "name": "Another IdP",
                "client_id": "x",
                "body": "x",
                "auth_endpoint": "https://other-idp.example.test/oauth/authorize",
                "validation_endpoint": "https://other-idp.example.test/oauth/userinfo",
                "issuer": "https://other-idp.example.test",
            }
        )
        self.user.oauth_uid = "someone-else"
        twin = self._cloud_user("mcp-auth-other-idp")
        twin.write({"oauth_provider_id": other.id, "oauth_uid": "4242"})
        self._refused(f"Bearer {self._token()}", 403)

    def test_every_enabled_provider_of_the_issuer_finds_its_users(self):
        """Two providers with the issuer: a user of either is found."""
        twin = self.provider.copy({"name": "MCP test IdP (twin)"})
        self.assertEqual(twin.issuer, self.provider.issuer)
        for provider in (self.provider, twin):
            with self.subTest(provider=provider.name):
                self.user.oauth_provider_id = provider
                principal = self.auth._authenticate(f"Bearer {self._token()}")
                self.assertEqual(principal.uid, self.user.id)

    def test_a_disabled_provider_lets_no_agent_in(self):
        """Signing in through a disabled provider is off for agents too."""
        self.provider.enabled = False
        with self.assertLogs(AUTH_LOGGER, level="ERROR"):
            self._refused(f"Bearer {self._token()}", 503)

    def test_a_credential_past_8_kib_is_401_not_a_crash(self):
        """A huge, deeply nested header is refused before anything reads it."""
        header = base64.urlsafe_b64encode(b"[" * 10_000).rstrip(b"=").decode()
        error = self._refused(f"Bearer {header}.e30.x", 401)
        self.assertTrue(error.token_presented)

    def test_the_superuser_is_refused(self):
        """No agent acts as the superuser: refused before anything else."""
        with self.assertRaises(AuthError) as caught:
            self.auth._check_user(self.env.ref("base.user_root"))
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("internal users only", caught.exception.message)

    def test_no_provider_for_the_issuer_is_503(self):
        """No provider with the issuer is a configuration error: 503, logged."""
        self.provider.unlink()
        with self.assertLogs(AUTH_LOGGER, level="ERROR"):
            self._refused(f"Bearer {self._token()}", 503)

    def test_without_issuer_a_token_is_401(self):
        """Without an issuer configured, a token opens nothing."""
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = False
        self._refused(f"Bearer {self._token()}", 401)

    def test_an_api_key_resolves_with_its_name(self):
        """An API key gives its user, and its name as the credential."""
        key = self._api_key(self.user, name="ci-bot")
        principal = self.auth._authenticate(f"Bearer {key}")
        self.assertEqual(
            (principal.uid, principal.credential), (self.user.id, "API key ci-bot")
        )

    def test_a_revoked_or_expired_key_is_401(self):
        """An expired or deleted API key is a 401."""
        key = self._api_key(self.user)
        self.env.cr.execute(
            "UPDATE res_users_apikeys SET expiration_date = now() - interval '1 day' "
            "WHERE user_id = %s",
            (self.user.id,),
        )
        self._refused(f"Bearer {key}", 401)
        key = self._api_key(self.user)
        self.env["res.users.apikeys"].sudo().search(
            [("user_id", "=", self.user.id)]
        ).unlink()
        self._refused(f"Bearer {key}", 401)

    def test_a_portal_user_with_a_key_is_403(self):
        """A portal user's API key is a 403: agents act for internal users."""
        portal = self._cloud_user("mcp-auth-portal", group="group_cloud_user")
        portal.group_ids = [(6, 0, [self.env.ref("base.group_portal").id])]
        error = self._refused(f"Bearer {self._api_key(portal)}", 403)
        self.assertIn("internal users only", error.message)

    def test_where_keys_are_not_allowed_the_answer_says_oauth(self):
        """Where API keys are off, the 401 says to connect with OAuth."""
        key = self._api_key(self.user)
        with patch.object(
            type(self.env["cloud.settings"]),
            "_mcp_api_keys_allowed",
            autospec=True,
            return_value=False,
        ):
            error = self._refused(f"Bearer {key}", 401)
        self.assertIn("connect with OAuth", error.message)

    def test_sixty_failures_from_an_address_block_it(self):
        """The sixtieth failure in a minute blocks that address only."""
        self.registry_enter_test_mode()
        auth = self.auth.with_context(force_rate_limit=True)
        for _i in range(AUTH_FAILURES_PER_MIN - 1):
            auth._count_failure("198.51.100.7")
        self.assertFalse(auth._auth_blocked("198.51.100.7"))
        auth._count_failure("198.51.100.7")
        self.assertTrue(auth._auth_blocked("198.51.100.7"))
        self.assertFalse(auth._auth_blocked("198.51.100.8"))

    def test_failures_from_one_ipv6_network_count_together(self):
        """IPv6 failures are counted per /64: one host has the whole block."""
        self.registry_enter_test_mode()
        auth = self.auth.with_context(force_rate_limit=True)
        for index in range(1, AUTH_FAILURES_PER_MIN + 1):
            auth._count_failure(f"2001:db8:1:2::{index:x}")
        self.assertTrue(auth._auth_blocked("2001:db8:1:2:ffff::1"))
        self.assertFalse(auth._auth_blocked("2001:db8:1:3::1"))

    def test_the_challenge_without_a_usable_resource_is_the_realm(self):
        """An issuer but no usable resource URL: a realm challenge, not a 500."""
        self.env["ir.config_parameter"].sudo().set_param(RESOURCE_PARAM, False)
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "nowhere")
        with self.assertLogs(AUTH_LOGGER, level="ERROR"):
            reply = self.auth._auth_reply(AuthError(401, "x"))
        self.assertEqual(
            reply.headers["WWW-Authenticate"], 'Bearer realm="incubacloud-panel"'
        )

    def test_the_401_challenge_points_to_the_resource_document(self):
        """With an issuer the challenge names the RFC 9728 document; else a realm."""
        reply = self.auth._auth_reply(AuthError(401, "x"))
        self.assertEqual(reply.status, 401)
        self.assertEqual(
            reply.headers["WWW-Authenticate"],
            'Bearer resource_metadata="https://panel.example.test'
            '/.well-known/oauth-protected-resource/mcp"',
        )
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = False
        reply = self.auth._auth_reply(AuthError(401, "x", token_presented=True))
        self.assertEqual(
            reply.headers["WWW-Authenticate"],
            'Bearer realm="incubacloud-panel", error="invalid_token"',
        )

    def test_a_503_carries_retry_after(self):
        """A 503 carries Retry-After and no challenge."""
        reply = self.auth._auth_reply(AuthError(503, "x", retry_after=30))
        self.assertEqual((reply.status, reply.headers["Retry-After"]), (503, "30"))
        self.assertNotIn("WWW-Authenticate", reply.headers)
