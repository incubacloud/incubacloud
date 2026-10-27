"""RFC 9728: which authorization server issues tokens for this panel.

Served at the path-inserted URL clients try first and at the root one.
Without an issuer there is no document (404): the MCP specification
requires at least one authorization server in it, and a panel with API
keys only has none.
"""

from odoo import SUPERUSER_ID, http
from odoo.http import request

from ..protocol.context import Reply
from ._reply import respond


class McpWellKnown(http.Controller):
    """The protected resource metadata of ``/mcp`` (RFC 9728)."""

    @http.route(
        [
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource",
        ],
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        save_session=False,
    )
    def protected_resource(self, **_kwargs):
        """Return the protected resource metadata of ``/mcp``."""
        if not request.db:
            return respond(Reply(404))
        Settings = request.env(user=SUPERUSER_ID)["cloud.settings"]
        issuer = Settings._mcp_issuer()
        if not issuer:
            return respond(Reply(404, {"error": "not_found"}))
        return respond(
            Reply(
                200,
                {
                    "resource": Settings._mcp_resource(),
                    "authorization_servers": [issuer],
                    "bearer_methods_supported": ["header"],
                    "resource_name": "IncubaCloud panel",
                },
                {"Cache-Control": "public, max-age=300"},
            )
        )
