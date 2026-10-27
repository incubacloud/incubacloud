"""The HTTP headers a 2026-07-28 POST mirrors from its JSON body.

A proxy may route on them, so the server refuses any request whose
headers say something different from the body it executes.
"""

import base64

from .errors import HEADER_MISMATCH, McpError

_PREFIX = "=?base64?"
_SUFFIX = "?="
#: Methods whose ``params.<field>`` travels in ``Mcp-Name``.
_NAMED_METHODS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def _mismatch(message):
    """Return the 400/-32020 error for *message*."""
    return McpError(400, HEADER_MISMATCH, f"Header mismatch: {message}")


def decode_header_value(value):
    """Return *value*, decoded when it uses the base64 sentinel.

    :param str value: the raw header value
    :rtype: str
    :raises McpError: 400/-32020 when the sentinel wraps invalid base64
    """
    if (
        value.startswith(_PREFIX)
        and value.endswith(_SUFFIX)
        and len(value) >= len(_PREFIX) + len(_SUFFIX)
    ):
        inner = value[len(_PREFIX) : -len(_SUFFIX)]
        try:
            return base64.b64decode(inner, validate=True).decode("utf-8")
        # binascii.Error, UnicodeDecodeError and the plain ValueError of a
        # non-ASCII value are all ValueErrors.
        except ValueError as exc:
            raise _mismatch("Mcp-Name is not valid base64") from exc
    return value


def check_modern_headers(headers, body, version):
    """Refuse a 2026-07-28 request whose headers do not mirror its body.

    :param headers: the request headers (a case-insensitive mapping)
    :param dict body: the decoded JSON-RPC request
    :param str version: ``io.modelcontextprotocol/protocolVersion`` of ``_meta``
    :raises McpError: 400/-32020 naming the first header that fails
    """
    header_version = headers.get("MCP-Protocol-Version")
    if not header_version:
        raise _mismatch("MCP-Protocol-Version is missing")
    if header_version != version:
        raise _mismatch(
            f"MCP-Protocol-Version '{header_version}' does not match _meta '{version}'"
        )
    method = body.get("method")
    header_method = headers.get("Mcp-Method")
    if header_method != method:
        raise _mismatch(
            f"Mcp-Method '{header_method}' does not match the body's '{method}'"
        )
    field = _NAMED_METHODS.get(method)
    if not field:
        return
    raw = headers.get("Mcp-Name")
    if raw is None:
        raise _mismatch("Mcp-Name is missing")
    expected = (body.get("params") or {}).get(field)
    if decode_header_value(raw) != expected:
        raise _mismatch(f"Mcp-Name does not match the body's '{expected}'")
