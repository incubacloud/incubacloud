"""Clients of 2025-03-26 to 2025-11-25: initialize and a signed session id."""

import time

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud_mcp.protocol.signing import sign, verify

from .common import McpHttpMixin, McpUsersMixin


@tagged("post_install", "-at_install")
class TestLegacyProtocol(McpUsersMixin, McpHttpMixin, HttpCase):
    """What a client of an earlier revision sends, and what it gets back.

    ``post_install``: its users need a partner, and a module installed
    later may add a column to it.
    """

    def setUp(self):
        """A developer with an API key, and the module's signing key."""
        super().setUp()
        self._resource_here()
        self.user = self._cloud_user("mcp-legacy-dev")
        self.key = self._api_key(self.user)
        self.secret = self.env["cloud.settings"]._mcp_secret()

    def _legacy(self, method, params=None, session=None, version=None, credential=None):
        """Send a legacy request (no ``_meta``), with an optional session id."""
        headers = {}
        if session:
            headers["Mcp-Session-Id"] = session
        if version:
            headers["MCP-Protocol-Version"] = version
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return self._post(body, credential or self.key, headers)

    def _initialize(self, version="2025-11-25", capabilities=None, credential=None):
        """Initialize; return ``(response, session id)``."""
        response = self._legacy(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": capabilities or {},
                "clientInfo": {"name": "legacy-agent", "version": "0.9"},
            },
            credential=credential,
        )
        return response, response.headers.get("Mcp-Session-Id")

    def test_initialize_negotiates_the_version(self):
        """A version this server speaks is answered as is; another, the newest."""
        for asked, answered in (
            ("2025-11-25", "2025-11-25"),
            ("2025-06-18", "2025-06-18"),
            ("2025-03-26", "2025-03-26"),
            ("2024-11-05", "2025-11-25"),
        ):
            with self.subTest(asked=asked):
                response, session = self._initialize(asked)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["result"]
                self.assertEqual(result["protocolVersion"], answered)
                self.assertEqual(result["serverInfo"]["name"], "incubacloud-panel")
                self.assertIn("tools", result["capabilities"])
                self.assertTrue(result["instructions"])
                self.assertTrue(session)

    def test_the_session_id_carries_the_capabilities_and_the_user(self):
        """The id is signed and holds version, elicitation, client name and user."""
        _response, session = self._initialize(
            capabilities={"elicitation": {"url": {}}, "roots": {}}
        )
        state = verify(self.secret, "session", session)
        self.assertEqual(state["c"], {"elicitation": {"url": {}}})
        self.assertEqual(
            (state["u"], state["n"], state["v"]),
            (self.user.id, "legacy-agent", "2025-11-25"),
        )

    def test_the_session_id_keeps_the_elicitation_modes_only(self):
        """What the client says inside each mode is not copied into the id."""
        for declared, kept in (
            (
                {"form": {"pad": "y" * 50_000}, "url": {}, "pad": "y" * 100_000},
                {"form": {}, "url": {}},
            ),
            ({}, {}),
        ):
            with self.subTest(kept=kept):
                _response, session = self._initialize(
                    capabilities={"elicitation": declared}
                )
                state = verify(self.secret, "session", session)
                self.assertEqual(state["c"], {"elicitation": kept})
                self.assertLess(len(session), 512)

    def test_initialized_is_202_and_ping_answers(self):
        """``notifications/initialized`` is 202; ``ping`` answers an empty result."""
        _response, session = self._initialize()
        notification = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self.key,
            {"Mcp-Session-Id": session},
        )
        self.assertEqual(notification.status_code, 202)
        ping = self._legacy("ping", session=session)
        self.assertEqual((ping.status_code, ping.json()["result"]), (200, {}))

    def test_without_the_session_id_it_is_400(self):
        """A legacy request other than ``initialize`` needs the session id."""
        self.assertEqual(self._legacy("ping").status_code, 400)

    def test_a_tampered_foreign_or_old_session_is_404(self):
        """A forged id, another user's, or one past a day: 404, initialize again."""
        _response, session = self._initialize()
        body, mac = session.split(".")
        tampered = f"{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{mac}"
        other = self._cloud_user("mcp-legacy-other")
        _response, foreign = self._initialize(credential=self._api_key(other))
        old = sign(
            self.secret,
            "session",
            {
                "v": "2025-11-25",
                "c": {},
                "n": "x",
                "u": self.user.id,
                "t": int(time.time()) - 25 * 3600,
            },
        )
        for bad in (tampered, foreign, old):
            with self.subTest(bad=bad[:12]):
                self.assertEqual(self._legacy("ping", session=bad).status_code, 404)

    def test_a_session_of_a_version_no_longer_spoken_is_404(self):
        """A well-signed id of a version this server does not speak is 404."""
        stale = sign(
            self.secret,
            "session",
            {
                "v": "2024-11-05",
                "c": {},
                "n": "x",
                "u": self.user.id,
                "t": int(time.time()),
            },
        )
        self.assertEqual(self._legacy("ping", session=stale).status_code, 404)

    def test_the_version_header_must_match_the_session_when_sent(self):
        """``MCP-Protocol-Version``, when sent, is the session's version."""
        _response, session = self._initialize("2025-06-18")
        response = self._legacy("ping", session=session, version="2025-06-18")
        self.assertEqual(response.status_code, 200)
        response = self._legacy("ping", session=session, version="2025-11-25")
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_method_is_a_json_rpc_error_with_200(self):
        """Earlier revisions get -32601 in a 200, as JSON-RPC over HTTP does."""
        _response, session = self._initialize()
        response = self._legacy("resources/list", session=session)
        self.assertEqual(
            (response.status_code, response.json()["error"]["code"]), (200, -32601)
        )

    def test_a_modern_client_cannot_call_initialize_or_ping(self):
        """``server/discover`` is the stateless revision's start: no session."""
        for method in ("initialize", "ping"):
            with self.subTest(method=method):
                response = self._modern(method, credential=self.key)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn("Mcp-Session-Id", response.headers)
