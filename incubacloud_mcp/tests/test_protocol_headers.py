"""Versions, JSON-RPC errors and the headers a 2026-07-28 POST mirrors."""

import base64

from werkzeug.datastructures import Headers

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.errors import (
    HEADER_MISMATCH,
    AuthError,
    McpError,
)
from odoo.addons.incubacloud_mcp.protocol.headers import (
    check_modern_headers,
    decode_header_value,
)
from odoo.addons.incubacloud_mcp.protocol.versions import (
    LEGACY,
    MODERN,
    SUPPORTED,
    negotiate_legacy,
)


def _call(name="get_instance"):
    """A tools/call body as a 2026-07-28 client sends it."""
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}


class TestVersions(BaseCase):
    def test_the_stateless_revision_comes_first(self):
        self.assertEqual(SUPPORTED[0], MODERN)
        self.assertEqual(MODERN, "2026-07-28")
        self.assertEqual(LEGACY, ("2025-11-25", "2025-06-18", "2025-03-26"))

    def test_initialize_answers_the_version_asked_or_the_newest_legacy(self):
        self.assertEqual(negotiate_legacy("2025-06-18"), "2025-06-18")
        self.assertEqual(negotiate_legacy("2024-11-05"), "2025-11-25")
        self.assertEqual(negotiate_legacy(None), "2025-11-25")


class TestErrors(BaseCase):
    def test_a_protocol_error_becomes_a_json_rpc_error(self):
        error = McpError(400, HEADER_MISMATCH, "nope", {"a": 1})
        self.assertEqual(error.status, 400)
        self.assertEqual(
            error.to_body(3),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "error": {"code": -32020, "message": "nope", "data": {"a": 1}},
            },
        )

    def test_an_auth_error_carries_its_status(self):
        error = AuthError(503, "down", retry_after=30)
        self.assertEqual(
            (error.status, error.retry_after, error.token_presented), (503, 30, False)
        )


class TestModernHeaders(BaseCase):
    def _headers(self, **overrides):
        """Headers that match :func:`_call`, with *overrides* applied."""
        values = {
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "get_instance",
        }
        values.update(overrides)
        return Headers([(k, v) for k, v in values.items() if v is not None])

    def _refused(self, headers, body=None):
        """Assert *headers* are refused with 400/-32020 and return the message."""
        with self.assertRaises(McpError) as caught:
            check_modern_headers(headers, body or _call(), MODERN)
        self.assertEqual(
            (caught.exception.status, caught.exception.code), (400, HEADER_MISMATCH)
        )
        return caught.exception.message

    def test_matching_headers_pass(self):
        check_modern_headers(self._headers(), _call(), MODERN)

    def test_header_names_are_case_insensitive(self):
        headers = Headers(
            [
                ("mcp-protocol-version", MODERN),
                ("MCP-METHOD", "tools/call"),
                ("mcp-name", "get_instance"),
            ]
        )
        check_modern_headers(headers, _call(), MODERN)

    def test_a_missing_or_different_protocol_version_is_refused(self):
        self.assertIn(
            "MCP-Protocol-Version",
            self._refused(self._headers(**{"MCP-Protocol-Version": None})),
        )
        self.assertIn(
            "MCP-Protocol-Version",
            self._refused(self._headers(**{"MCP-Protocol-Version": "2025-11-25"})),
        )

    def test_a_different_method_is_refused(self):
        self.assertIn(
            "Mcp-Method", self._refused(self._headers(**{"Mcp-Method": "tools/list"}))
        )

    def test_tools_call_needs_the_tool_name(self):
        self.assertIn("Mcp-Name", self._refused(self._headers(**{"Mcp-Name": None})))
        self.assertIn(
            "Mcp-Name", self._refused(self._headers(**{"Mcp-Name": "delete_instance"}))
        )

    def test_a_base64_name_is_decoded_before_comparing(self):
        encoded = "=?base64?" + base64.b64encode(b"get_instance").decode() + "?="
        check_modern_headers(self._headers(**{"Mcp-Name": encoded}), _call(), MODERN)
        self.assertEqual(decode_header_value("plain"), "plain")

    def test_invalid_base64_is_refused(self):
        for value in ("=?base64?%%%?=", "=?base64?ñ?="):
            with self.subTest(value=value):
                with self.assertRaises(McpError) as caught:
                    decode_header_value(value)
                self.assertIn("not valid base64", caught.exception.message)
                self.assertIn(
                    "not valid base64",
                    self._refused(self._headers(**{"Mcp-Name": value})),
                )

    def test_other_methods_need_no_name(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        check_modern_headers(
            self._headers(**{"Mcp-Method": "tools/list", "Mcp-Name": None}),
            body,
            MODERN,
        )
