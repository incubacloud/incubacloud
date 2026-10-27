"""The stateless revision: _meta, mirrored headers, versions, methods."""

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud_mcp.protocol.versions import MODERN, SUPPORTED

from .common import McpHttpMixin, McpUsersMixin


@tagged("post_install", "-at_install")
class TestModernProtocol(McpUsersMixin, McpHttpMixin, HttpCase):
    """What a 2026-07-28 client sends, and what it gets back.

    ``post_install``: its users need a partner, and a module installed
    later may add a column to it.
    """

    def setUp(self):
        """A developer with an API key, on this test server's ``/mcp``."""
        super().setUp()
        self._resource_here()
        self.key = self._api_key(self._cloud_user("mcp-modern-dev"))

    def _error(self, response, status, code):
        """Assert *response* is a JSON-RPC error *code* with HTTP *status*."""
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body["error"]["code"], code)
        return body["error"]

    def test_discover_answers_versions_capabilities_and_identity(self):
        """``server/discover`` lists versions, capabilities, cache and identity."""
        result = self._modern("server/discover", credential=self.key).json()["result"]
        self.assertEqual(result["supportedVersions"], list(SUPPORTED))
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual((result["ttlMs"], result["cacheScope"]), (300000, "private"))
        self.assertTrue(result["instructions"])
        info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
        self.assertEqual(info["name"], "incubacloud-panel")
        self.assertTrue(info["version"])

    def test_the_answer_echoes_the_request_id(self):
        """The response carries the request's id, whatever its type."""
        for request_id in (7, "abc"):
            with self.subTest(request_id=request_id):
                response = self._modern(
                    "server/discover", credential=self.key, request_id=request_id
                )
                self.assertEqual(response.json()["id"], request_id)

    def test_an_id_that_is_not_a_string_or_an_integer_is_refused(self):
        """Null, an object, a list, a boolean or a fraction is no id: -32600."""
        for request_id in (None, {"a": 1}, [1], True, 1.5):
            with self.subTest(request_id=request_id):
                response = self._modern(
                    "server/discover", credential=self.key, request_id=request_id
                )
                self._error(response, 400, -32600)

    def test_a_notification_is_202_without_body(self):
        """A message without an id is accepted with 202 and an empty body."""
        response = self._post(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
            self.key,
        )
        self.assertEqual((response.status_code, response.text), (202, ""))

    def test_headers_that_do_not_mirror_the_body_are_400_header_mismatch(self):
        """A version, method or name header that differs from the body: -32020."""
        for headers in ({"MCP-Protocol-Version": ""}, {"Mcp-Method": "tools/list"}):
            with self.subTest(headers=headers):
                response = self._modern(
                    "server/discover", credential=self.key, headers=headers
                )
                self._error(response, 400, -32020)
        response = self._modern(
            "tools/call",
            {"name": "search"},
            credential=self.key,
            headers={"Mcp-Name": "other"},
        )
        self._error(response, 400, -32020)

    def test_an_unknown_version_is_400_with_the_supported_list(self):
        """A version this server does not speak: -32022 with the supported ones."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "1900-01-01",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
        headers = {
            "MCP-Protocol-Version": "1900-01-01",
            "Mcp-Method": "server/discover",
        }
        error = self._error(self._post(body, self.key, headers), 400, -32022)
        self.assertEqual(error["data"]["supported"], list(SUPPORTED))

    def test_an_unknown_method_is_404(self):
        """A method this server does not have: 404 and -32601."""
        self._error(self._modern("resources/list", credential=self.key), 404, -32601)

    def test_meta_must_carry_version_and_capabilities(self):
        """A 2026-07-28 client whose ``_meta`` lacks the version or the
        capabilities: -32602."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
        headers = {"Mcp-Method": "server/discover", "MCP-Protocol-Version": MODERN}
        self._error(self._post(body, self.key, headers), 400, -32602)
        body["params"]["_meta"] = {"io.modelcontextprotocol/protocolVersion": MODERN}
        self._error(self._post(body, self.key, headers), 400, -32602)

    def test_params_and_meta_must_be_objects(self):
        """``params`` or ``_meta`` that are not objects: -32602, not a crash."""
        for params in (["x"], {"_meta": "x"}):
            with self.subTest(params=params):
                body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
                body["params"] = params
                response = self._post(body, self.key, {"Mcp-Method": "server/discover"})
                self._error(response, 400, -32602)

    def test_a_batch_is_refused(self):
        """One JSON-RPC message per POST: a batch is -32600, with no id."""
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "server/discover"}]
        response = self._post(batch, self.key)
        self._error(response, 400, -32600)
        self.assertNotIn("id", response.json())
