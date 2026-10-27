"""The endpoint's door: method, origin, body and credential."""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import JwtMixin, McpHttpMixin, McpUsersMixin

CONTROLLER_LOGGER = "odoo.addons.incubacloud_mcp.controllers.mcp"


@tagged("post_install", "-at_install")
class TestTheEndpoint(JwtMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """``POST /mcp`` before any MCP work: who may knock, and how.

    ``post_install``: its users need a partner, and a module installed
    later may add a column to it.
    """

    def setUp(self):
        """Point the panel at the test issuer; a developer with a token and a key."""
        super().setUp()
        self.resource = self._resource_here()
        self._setup_issuer(self.resource)
        self.provider = self._configure_issuer(self.resource)
        self.user = self._cloud_user("mcp-http-dev")
        self.user.write({"oauth_provider_id": self.provider.id, "oauth_uid": "4242"})
        self.key = self._api_key(self.user)

    def _discover(self, credential=None, headers=None):
        """``server/discover`` with *credential*."""
        return self._modern("server/discover", credential=credential, headers=headers)

    def _proxies(self, ranges):
        """Make *ranges* the trusted proxies the endpoint reads.

        Patched rather than written: the layer that runs the panel may
        supply ranges of its own. ``127.0.0.0/8`` holds the test server's
        address, which is then the client's own.
        """
        patcher = patch.object(
            type(self.env["cloud.settings"]),
            "_effective_trusted_proxy_ranges",
            autospec=True,
            return_value=ranges,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_and_delete_are_405_and_say_post(self):
        """GET and DELETE are 405 with ``Allow: POST``: there is no stream."""
        url = f"{self.base_url()}/mcp"
        database = {"X-Odoo-Database": self.env.cr.dbname}
        for response in (
            self.url_open("/mcp", headers=database),
            self.opener.delete(url, headers=database, timeout=12),
        ):
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response.headers.get("Allow"), "POST")

    def test_without_credential_the_challenge_names_the_resource_document(self):
        """No credential: 401 whose challenge points to the RFC 9728 document."""
        response = self._discover()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.headers["WWW-Authenticate"],
            f'Bearer resource_metadata="{self.base_url()}'
            '/.well-known/oauth-protected-resource/mcp"',
        )

    def test_a_session_cookie_alone_is_401(self):
        """The panel's session cookie opens nothing here."""
        self.authenticate("mcp-http-dev", "mcp-http-dev")
        self.assertEqual(self._discover().status_code, 401)

    def test_a_token_opens_it(self):
        """An access token of the issuer gets an answer from the server."""
        response = self._discover(self._token())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(
            result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
            "incubacloud-panel",
        )

    def test_an_api_key_opens_it(self):
        """One of the user's API keys gets an answer too."""
        self.assertEqual(self._discover(self.key).status_code, 200)

    def test_the_answer_is_never_cached_and_sets_no_cookie(self):
        """The answer says ``no-store`` and the endpoint keeps no session."""
        response = self._discover(self.key)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertNotIn("Set-Cookie", response.headers)

    def test_a_token_without_a_local_user_is_403_without_a_challenge(self):
        """A good token without a local user: 403 that says what to do."""
        response = self._discover(self._token(sub="9999"))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("WWW-Authenticate", response.headers)
        self.assertIn("Sign in to the panel once", response.json()["error_description"])

    def test_an_unreachable_issuer_is_503_with_retry_after(self):
        """An issuer that cannot be asked is a 503; API keys still work."""
        self._issuer_down = True
        with self.assertLogs("odoo.addons.incubacloud_mcp.models.mcp_auth", "WARNING"):
            response = self._discover(self._token())
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.headers.get("Retry-After"))
        self.assertEqual(self._discover(self.key).status_code, 200)

    def test_a_foreign_origin_is_403_the_panel_s_own_is_not(self):
        """A browser ``Origin`` other than the panel's is refused."""
        foreign = self._discover(self.key, {"Origin": "https://evil.example.test"})
        self.assertEqual(foreign.status_code, 403)
        own = self._discover(self.key, {"Origin": self.base_url()})
        self.assertEqual(own.status_code, 200)

    def test_only_json_is_accepted(self):
        """Another content type is 415; a body that is not JSON, -32700."""
        response = self._post({}, self.key, {"Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        response = self._post(None, self.key, raw="{not json")
        self.assertEqual(
            (response.status_code, response.json()["error"]["code"]), (400, -32700)
        )

    def test_a_body_too_big_or_too_deep_is_refused(self):
        """Past 256 KiB the body is not read; nested past the parser, -32700."""
        response = self._post(None, self.key, raw=" " * (256 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        response = self._post(None, self.key, raw="[" * 100_000)
        self.assertEqual(
            (response.status_code, response.json()["error"]["code"]), (400, -32700)
        )

    def test_an_address_over_its_failures_is_429(self):
        """Behind declared proxies, an address over its failures is refused first."""
        self._proxies(["127.0.0.0/8"])
        Auth = type(self.env["incubacloud.mcp.auth"])
        with patch.object(Auth, "_auth_blocked", autospec=True, return_value=True):
            response = self._discover(self.key)
        self.assertEqual(
            (response.status_code, response.headers.get("Retry-After")), (429, "60")
        )

    def test_without_declared_proxies_failures_are_counted_not_refused(self):
        """No trusted proxy declared: the address may be the proxy's, shared
        by every client. It is never refused; its failures are still counted,
        and one over the limit is logged."""
        self._proxies([])
        Auth = type(self.env["incubacloud.mcp.auth"])
        with (
            patch.object(Auth, "_auth_blocked", autospec=True, return_value=True),
            patch.object(Auth, "_count_failure", autospec=True) as count,
        ):
            self.assertEqual(self._discover(self.key).status_code, 200)
            count.assert_not_called()
            with self.assertLogs(CONTROLLER_LOGGER, "WARNING") as logs:
                self.assertEqual(self._discover("not-a-key").status_code, 401)
        self.assertEqual(count.call_count, 1)
        self.assertEqual(count.call_args.args[1], "127.0.0.1")
        self.assertIn("127.0.0.1", logs.output[0])
        self.assertIn("trusted proxies", logs.output[0])

    def test_a_failed_authentication_is_counted_for_its_address(self):
        """A 401 counts against the address; a 403 or a 503 does not."""
        self._proxies(["127.0.0.0/8"])
        Auth = type(self.env["incubacloud.mcp.auth"])
        with patch.object(Auth, "_count_failure", autospec=True) as count:
            self.assertEqual(self._discover("not-a-key").status_code, 401)
            self.assertEqual(self._discover(self._token(sub="9999")).status_code, 403)
        self.assertEqual(count.call_count, 1)
        self.assertEqual(count.call_args.args[1], "127.0.0.1")

    def test_both_resource_documents_name_the_issuer(self):
        """Both RFC 9728 URLs name this resource and the configured issuer."""
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource",
        ):
            with self.subTest(path=path):
                response = self.url_open(
                    path, headers={"X-Odoo-Database": self.env.cr.dbname}
                )
                self.assertEqual(response.status_code, 200)
                document = response.json()
                self.assertEqual(document["resource"], self.resource)
                self.assertEqual(
                    document["authorization_servers"], ["https://idp.example.test"]
                )
                self.assertEqual(document["bearer_methods_supported"], ["header"])
                self.assertTrue(document["resource_name"])

    def test_without_issuer_there_is_no_document_and_the_challenge_has_a_realm(self):
        """API keys only: no RFC 9728 document, and a realm in the challenge."""
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = False
        response = self.url_open(
            "/.well-known/oauth-protected-resource/mcp",
            headers={"X-Odoo-Database": self.env.cr.dbname},
        )
        self.assertEqual(response.status_code, 404)
        challenge = self._discover().headers["WWW-Authenticate"]
        self.assertEqual(challenge, 'Bearer realm="incubacloud-panel"')
