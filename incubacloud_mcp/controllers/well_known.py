"""RFC 9728: which authorization server issues tokens for this panel.

Served at the path-inserted URLs clients try first — of ``/cloud/mcp`` and
of ``/mcp`` — and at the root one. The two last are aliases, with ``/mcp``:
they all leave the routing map when another installed module declares one
(``models/ir_http.py``). A client takes a document only when its resource
is the URL it called (RFC 9728, §3.3), so each path-inserted document names
the URL of its path, and the root one names ``…/mcp``, the address that
clients connected before ``/cloud/mcp`` call and check it against. A
resource on a path neither route has (a proxy that maps it) is named by
the three, as before (``cloud.settings._mcp_resource_at``).

Without an issuer there is no document (404): the MCP specification
requires at least one authorization server in it, and a panel with API
keys only has none. Nor without a usable MCP URL: a document that names no
resource would only mislead the client.
"""

import logging
from odoo import SUPERUSER_ID, http
from odoo.http import request

from ..protocol.canonical import ALIAS_PATH, MCP_PATH, METADATA_PATH
from ..protocol.context import Reply
from ._reply import respond

_logger = logging.getLogger(__name__)


class McpWellKnown(http.Controller):
    """The protected resource metadata of the MCP (RFC 9728)."""

    @http.route(
        [
            METADATA_PATH + MCP_PATH,
            METADATA_PATH + ALIAS_PATH,
            METADATA_PATH,
        ],
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        save_session=False,
    )
    def protected_resource(self, **_kwargs):
        """Return the protected resource metadata of the panel's MCP."""
        if not request.db:
            return respond(Reply(404))
        Settings = request.env(user=SUPERUSER_ID)["cloud.settings"]
        inserted = request.httprequest.path[len(METADATA_PATH) :].rstrip("/")
        issuer = Settings._mcp_issuer()
        resource = Settings._mcp_resource_at(inserted or ALIAS_PATH)
        if not issuer:
            _logger.debug("[mcp] no agents' issuer: no protected resource document")
            return respond(Reply(404, {"error": "not_found"}))
        if not resource:
            _logger.warning(
                "[mcp] agents' issuer %s is set but this panel has no usable MCP "
                "URL (incubacloud.mcp.resource, web.base.url): no protected "
                "resource document",
                issuer,
            )
            return respond(Reply(404, {"error": "not_found"}))
        return respond(
            Reply(
                200,
                {
                    "resource": resource,
                    "authorization_servers": [issuer],
                    "bearer_methods_supported": ["header"],
                    "resource_name": "IncubaCloud panel",
                },
                {"Cache-Control": "public, max-age=300"},
            )
        )
