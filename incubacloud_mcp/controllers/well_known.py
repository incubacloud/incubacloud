"""RFC 9728: which authorization server issues tokens for this panel.

Served at the path-inserted URLs clients try first — of ``/cloud/mcp`` and
of ``/mcp`` — and at the root one. The two last are aliases: they leave the
routing map when another installed module declares them
(``models/ir_http.py``). A path-inserted document answers only for the
panel's resource: the other one is 404, as a client must not take a
document whose resource is not the URL it asked for (RFC 9728, §3.3). A
resource on a path neither route has (a proxy that maps it) is answered
on both, as before.

Without an issuer there is no document (404): the MCP specification
requires at least one authorization server in it, and a panel with API
keys only has none. Nor without a usable MCP URL: a document that names no
resource would only mislead the client.
"""

import logging
from urllib.parse import urlsplit

from odoo import SUPERUSER_ID, http
from odoo.http import request

from ..protocol.canonical import ALIAS_PATH, MCP_PATH, METADATA_PATH
from ..protocol.context import Reply
from ._reply import respond

_logger = logging.getLogger(__name__)

#: The paths of the MCP routes, each with a path-inserted document.
_SERVED = frozenset({MCP_PATH, ALIAS_PATH})


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
        issuer, resource = Settings._mcp_issuer(), Settings._mcp_resource()
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
        inserted = request.httprequest.path[len(METADATA_PATH) :].rstrip("/")
        resource_path = urlsplit(resource).path
        if inserted and resource_path in _SERVED and inserted != resource_path:
            _logger.debug(
                "[mcp] no protected resource document at %s: this panel's MCP is %s",
                request.httprequest.path,
                resource,
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
