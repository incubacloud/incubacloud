"""A credential becomes a panel user — or a refusal that says why."""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

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
        for header in (None, "", "Basic abc", "Bearer "):
            with self.subTest(header=header):
                self.assertFalse(self._refused(header, 401).token_presented)

    def test_a_token_resolves_to_its_user(self):
        principal = self.auth._authenticate(f"Bearer {self._token()}")
        self.assertEqual(
            (principal.uid, principal.credential), (self.user.id, CLIENT_ID)
        )

    def test_an_issuer_that_ends_in_a_slash_like_auth0s_works(self):
        self._issuer_published = ISSUER + "/"
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = ISSUER + "/"
        self.provider.issuer = ISSUER + "/"
        principal = self.auth._authenticate(f"Bearer {self._token(iss=ISSUER + '/')}")
        self.assertEqual(principal.uid, self.user.id)

    def test_a_token_without_a_local_user_says_sign_in_once(self):
        error = self._refused(f"Bearer {self._token(sub='9999')}", 403)
        self.assertIn("Sign in to the panel once", error.message)

    def test_an_inactive_user_or_one_without_role_is_403(self):
        self.user.active = False
        self._refused(f"Bearer {self._token()}", 403)
        self.user.active = True
        developer = self.env.ref("incubacloud.group_cloud_developer")
        self.user.group_ids = [(3, developer.id)]
        self._refused(f"Bearer {self._token()}", 403)

    def test_no_provider_for_the_issuer_is_503(self):
        self.provider.unlink()
        with self.assertLogs(AUTH_LOGGER, level="ERROR"):
            self._refused(f"Bearer {self._token()}", 503)

    def test_without_issuer_a_token_is_401(self):
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = False
        self._refused(f"Bearer {self._token()}", 401)

    def test_an_api_key_resolves_with_its_name(self):
        key = self._api_key(self.user, name="ci-bot")
        principal = self.auth._authenticate(f"Bearer {key}")
        self.assertEqual(
            (principal.uid, principal.credential), (self.user.id, "API key ci-bot")
        )

    def test_a_revoked_or_expired_key_is_401(self):
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
        portal = self._cloud_user("mcp-auth-portal", group="group_cloud_user")
        portal.group_ids = [(6, 0, [self.env.ref("base.group_portal").id])]
        self._refused(f"Bearer {self._api_key(portal)}", 403)

    def test_where_keys_are_not_allowed_the_answer_says_oauth(self):
        key = self._api_key(self.user)
        with patch.object(
            type(self.env["cloud.settings"]),
            "_mcp_api_keys_allowed",
            return_value=False,
        ):
            error = self._refused(f"Bearer {key}", 401)
        self.assertIn("connect with OAuth", error.message)

    def test_sixty_failures_from_an_address_block_it(self):
        self.registry_enter_test_mode()
        auth = self.auth.with_context(force_rate_limit=True)
        for _i in range(AUTH_FAILURES_PER_MIN - 1):
            auth._count_failure("198.51.100.7")
        self.assertFalse(auth._auth_blocked("198.51.100.7"))
        auth._count_failure("198.51.100.7")
        self.assertTrue(auth._auth_blocked("198.51.100.7"))
        self.assertFalse(auth._auth_blocked("198.51.100.8"))

    def test_the_401_challenge_points_to_the_resource_document(self):
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
        reply = self.auth._auth_reply(AuthError(503, "x", retry_after=30))
        self.assertEqual((reply.status, reply.headers["Retry-After"]), (503, "30"))
        self.assertNotIn("WWW-Authenticate", reply.headers)
