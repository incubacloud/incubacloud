"""Shared fixtures of the MCP tests: an issuer of their own, and users."""

import json
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from joserfc import jws
from joserfc.jwk import RSAKey

from odoo import fields
from odoo.tests.common import new_test_user

from odoo.addons.incubacloud_mcp.controllers import _issuer_metadata, _jwks_cache
from odoo.addons.incubacloud_mcp.models.cloud_settings import RESOURCE_PARAM
from odoo.addons.incubacloud_mcp.protocol.canonical import canonical_resource
from odoo.addons.incubacloud_mcp.protocol.redact import secret_variants
from odoo.addons.incubacloud_mcp.protocol.versions import MODERN

ISSUER = "https://idp.example.test"
JWKS_URI = ISSUER + "/oauth/jwks"
CLIENT_ID = "https://client.example.test/oauth/client.json"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"


def http_response(status, data=None, body=None, announce=True):
    """A ``requests`` response with *status* and the JSON *data*.

    :param bytes body: a raw body instead of *data*'s JSON
    :param bool announce: whether ``Content-Length`` gives the body's size
    """
    raw = body if body is not None else json.dumps(data).encode()
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.headers = {"Content-Length": str(len(raw))} if announce else {}
    response.content = raw
    response.iter_content.return_value = [raw]
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
        # What the issuer calls itself: Auth0's ends in a slash.
        self._issuer_published = ISSUER
        # More public keys the issuer publishes (another algorithm's).
        self._extra_keys = []
        self._jwks_calls = 0
        patcher = patch.object(
            requests, "get", autospec=True, side_effect=self._issuer_get
        )
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
            return http_response(
                200, {"issuer": self._issuer_published, "jwks_uri": JWKS_URI}
            )
        if url == JWKS_URI:
            self._jwks_calls += 1
            keys = [self._rsa_key, *self._extra_keys]
            return http_response(
                200, {"keys": [key.as_dict(private=False) for key in keys]}
            )
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
    """Users, API keys and the issuer's configuration.

    A test case that creates users runs ``post_install``: a user needs a
    partner, and a module installed later may add a column to it.
    """

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
                    "enabled": True,
                }
            )
        )


class McpHttpMixin:
    """Talk to ``/mcp`` over real HTTP, as an agent does. For an ``HttpCase``."""

    def _resource_here(self):
        """The canonical ``/mcp`` URL of the test server; also stored as the panel's."""
        resource = canonical_resource(self.base_url() + "/mcp")
        self.env["ir.config_parameter"].sudo().set_param(RESOURCE_PARAM, resource)
        return resource

    def _post(self, body, credential=None, headers=None, raw=None):
        """POST *body* (or the *raw* text) to ``/mcp``; return the response."""
        sent = {
            "Content-Type": "application/json",
            "X-Odoo-Database": self.env.cr.dbname,
        }
        if credential:
            sent["Authorization"] = f"Bearer {credential}"
        sent.update(headers or {})
        data = raw if raw is not None else json.dumps(body)
        return self.url_open("/mcp", data=data, headers=sent)

    def _modern(
        self,
        method,
        params=None,
        credential=None,
        capabilities=None,
        request_id=1,
        headers=None,
    ):
        """Send a 2026-07-28 request with consistent ``_meta`` and headers."""
        params = dict(params or {})
        params["_meta"] = {
            META_VERSION: MODERN,
            META_CAPABILITIES: capabilities if capabilities is not None else {},
            META_CLIENT: {"name": "test-agent", "version": "1.0"},
        }
        sent = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
        if method == "tools/call":
            sent["Mcp-Name"] = params["name"]
        sent.update(headers or {})
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        return self._post(body, credential, sent)

    def _call_tool(self, credential, name, arguments=None, capabilities=None, **params):
        """Call tool *name* in 2026-07-28 and return the JSON-RPC ``result``."""
        response = self._modern(
            "tools/call",
            {"name": name, "arguments": arguments or {}} | params,
            credential,
            capabilities,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def _tool_names(self, credential):
        """Return the names ``tools/list`` gives *credential*'s user."""
        listing = self._modern("tools/list", credential=credential).json()["result"]
        return {tool["name"] for tool in listing["tools"]}

    def _assert_refused_by_role(self, credential, name, arguments):
        """Check that tool *name* is neither listed nor run for this user:
        the call is refused before any route of the panel is reached."""
        self.assertNotIn(name, self._tool_names(credential))
        Dispatch = type(self.env["incubacloud.mcp.dispatch"])
        with patch.object(Dispatch, "_call", autospec=True, return_value={}) as call:
            result = self._call_tool(credential, name, arguments)
        self.assertTrue(result["isError"])
        call.assert_not_called()
        return result

    @staticmethod
    def _payload(result):
        """Return the JSON a successful tool result carries."""
        text = result["content"][-1]["text"]
        if text.startswith("<untrusted_data>\n"):
            text = text[len("<untrusted_data>\n") : -len("\n</untrusted_data>")]
        return json.loads(text)


#: What a client of the stateless revision declares to be asked through a form.
FORM_CAPABILITIES = {"elicitation": {"form": {}}}
#: What a client declares to be asked through a link only.
URL_CAPABILITIES = {"elicitation": {"url": {}}}
#: The answer of a person who approves in the client's form.
ACCEPT = {"confirm": {"action": "accept", "content": {"confirm": True}}}


class ConfirmMixin:
    """Approve through the client's form, as a 2026-07-28 client does.

    For a test case that also mixes in :class:`McpHttpMixin`.
    """

    def _confirmed_call(self, credential, name, arguments):
        """Call *name*, accept its form, and return the final result."""
        first = self._call_tool(credential, name, arguments, FORM_CAPABILITIES)
        self.assertEqual(first.get("resultType"), "input_required", first)
        return self._call_tool(
            credential,
            name,
            arguments,
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=ACCEPT,
        )


HOST_PASSWORD = "mcp-host-pass-4821"
PG_PASSWORD = "mcp-pg-pass-9137"
ADMIN_PASSWORD = "mcp-admin-pass-5520"
S3_SECRET = "mcp-s3-secret-7713"
METRICS_TOKEN = "mcp-metrics-token-6621"
WEBHOOK_SECRET = "mcp-webhook-secret-3390"
#: A line of the GitHub App's private key: a key printed line by line.
GITHUB_KEY_LINE = "TUNQLWZpeHR1cmUtcHJpdmF0ZS1rZXktbGluZS1vZi1zaXh0eS1mb3VyLWNoYXJzLi4"
GITHUB_KEY = (
    f"-----BEGIN RSA PRIVATE KEY-----\n{GITHUB_KEY_LINE}\n-----END RSA PRIVATE KEY-----"
)
SECRETS = (
    HOST_PASSWORD,
    PG_PASSWORD,
    ADMIN_PASSWORD,
    S3_SECRET,
    METRICS_TOKEN,
    WEBHOOK_SECRET,
    GITHUB_KEY_LINE,
)


class StandalonePanelMixin:
    """Run the test as a panel nobody manages from above.

    A layer over core (the tenant's) may say the issuer and the resource
    URL are set by the platform and hide the observability settings. The
    tests of a panel that edits them itself say what they are about: this
    holds the panel to that whatever is installed, and changes nothing
    where nothing is. The tests of the managed panel patch the other way
    inside their own body (:meth:`_managed_panel`).
    """

    def setUp(self):
        """Hold ``_mcp_settings_managed`` to False for the test."""
        super().setUp()
        self._managed = self.startPatcher(
            patch.object(
                type(self.env["cloud.settings"]),
                "_mcp_settings_managed",
                autospec=True,
                return_value=False,
            )
        )

    @contextmanager
    def _managed_panel(self):
        """Run the block as a panel whose issuer and URL are set from above.

        Not a second patch: the mixin's already stands in for the method.
        """
        self._managed.return_value = True
        try:
            yield
        finally:
            self._managed.return_value = False


class PanelFixtureMixin:
    """A server, a project and its two instances, with their secrets set."""

    def _panel_fixture(self):
        """Create the records; members of the project are added by each test."""
        self.host = self.env["cloud.host"].create(
            {
                "name": "mcp-host",
                "ip_address": "10.0.0.90",
                "user": "root",
                "wildcard_domain": "mcp.example.com",
                "login_type": "password",
                "password": HOST_PASSWORD,
            }
        )
        self.project = self.env["cloud.project"].create(
            {"name": "MCP Acme", "remote_folder": "mcpacme"}
        )
        self.production = self.env["cloud.instance"].create(
            {
                "name": "mcp-acme-prod",
                "project_id": self.project.id,
                "host_id": self.host.id,
                "environment": "production",
                "postgres_password": PG_PASSWORD,
                "odoo_admin_password": ADMIN_PASSWORD,
            }
        )
        self.staging = self.env["cloud.instance"].create(
            {
                "name": "mcp-acme-staging",
                "project_id": self.project.id,
                "host_id": self.host.id,
                "environment": "staging",
            }
        )
        self.backend = (
            self.env["cloud.backup.backend"]
            .sudo()
            .create(
                {
                    "name": "mcp-s3",
                    "backend_type": "s3",
                    "s3_bucket": "mcp-bucket",
                    "s3_access_key_id": "AKIAMCPTEST",
                    "s3_secret_access_key": S3_SECRET,
                    "s3_endpoint_url": "https://s3.example.test",
                }
            )
        )
        self._settings_secrets()

    def _settings_secrets(self):
        """Give the settings their secrets: the metrics token and the GitHub App's."""
        self.env[
            "cloud.settings"
        ]._get_system().metrics_remote_write_token = METRICS_TOKEN
        App = self.env["cloud.github.app"].sudo()
        vals = {
            "app_id": "4711",
            "installation_id": "8080",
            "private_key": GITHUB_KEY,
            "webhook_secret": WEBHOOK_SECRET,
        }
        self.github_app = App.search([], limit=1)
        if self.github_app:
            self.github_app.write(vals)
        else:
            self.github_app = App.create(vals)

    def _assert_no_secret(self, text):
        """Fail if a fixture secret appears in *text*: plain, URL-encoded or base64."""
        for secret in SECRETS:
            for form in secret_variants(secret):
                self.assertNotIn(form, text)
