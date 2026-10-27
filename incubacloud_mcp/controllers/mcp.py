"""``POST /mcp``: the MCP endpoint of the panel.

Only the transport lives here — method, database, origin, content type,
the credential — and the shape of the answer (always JSON, never SSE).
What a message means is ``incubacloud.mcp.server``'s business.

``auth='none'`` on purpose. ``auth='user'`` would accept the panel's
session cookie, and ``auth='bearer'`` falls back to it when the header is
missing: with ``SameSite=Lax`` and panels sharing a registrable domain, a
cookie-authenticated endpoint is CSRF from any sibling subdomain. The
cookie is ignored; only ``Authorization: Bearer`` counts. A self-hosted
server with several databases needs a ``dbfilter`` that picks one by
host name: without a database the endpoint answers 404.

Failed authentications are counted per address, and an address over the
limit is refused before its credential is read — but only where the
installation declared its proxies. Without them the address may be the
proxy's, shared by every client: refusing it would lock everyone out at
once, so the failures are still counted and going over the limit is
only logged.
"""

import json
import logging

from odoo import SUPERUSER_ID, http
from odoo.http import request

from odoo.addons.incubacloud.controllers._client_ip import client_ip

from ..models.mcp_auth import AUTH_FAILURES_PER_MIN
from ..protocol.canonical import same_origin
from ..protocol.context import Reply
from ..protocol.errors import INVALID_REQUEST, PARSE_ERROR, AuthError, McpError
from ._reply import respond

_logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024


class McpController(http.Controller):
    """The MCP endpoint: transport and credential, then the server model."""

    @http.route(
        "/mcp",
        type="http",
        auth="none",
        methods=["POST", "GET", "DELETE"],
        csrf=False,
        save_session=False,
        # Tool calls write (jobs, audit lines) and so does a failure's
        # count: never a read-only cursor.
        readonly=False,
        max_content_length=MAX_BODY_BYTES,
    )
    def mcp(self, **_kwargs):
        """Answer one JSON-RPC message, as the user its credential names."""
        httprequest = request.httprequest
        if httprequest.method != "POST":
            return respond(Reply(405, None, {"Allow": "POST"}))
        if not request.db:
            return respond(Reply(404))
        system = request.env(user=SUPERUSER_ID)
        origin = httprequest.headers.get("Origin")
        if origin and not same_origin(origin, system["cloud.settings"]._mcp_resource()):
            error = McpError(403, INVALID_REQUEST, "Origin not allowed")
            return respond(Reply(403, error.to_body(None)))
        if httprequest.mimetype != "application/json":
            error = McpError(
                415, INVALID_REQUEST, "Content-Type must be application/json"
            )
            return respond(Reply(415, error.to_body(None)))
        Auth = system["incubacloud.mcp.auth"]
        ip = client_ip()
        reliable = bool(system["cloud.settings"]._effective_trusted_proxy_ranges())
        if reliable and Auth._auth_blocked(ip):
            return respond(
                Reply(
                    429,
                    {
                        "error": "too_many_requests",
                        "error_description": (
                            "Too many failed authentications from this address."
                        ),
                    },
                    {"Retry-After": "60"},
                )
            )
        try:
            principal = Auth._authenticate(httprequest.headers.get("Authorization"))
        except AuthError as error:
            if error.status == 401:
                self._note_failure(Auth, ip, reliable)
            return respond(Auth._auth_reply(error))
        context = request.env(user=principal.uid)["res.users"].context_get()
        request.update_env(user=principal.uid, context=context)
        try:
            message = json.loads(httprequest.get_data(as_text=True) or "null")
        # A body nested deeper than the parser's stack is as unreadable
        # as one that is not JSON.
        except (ValueError, RecursionError):
            error = McpError(400, PARSE_ERROR, "Parse error")
            return respond(Reply(400, error.to_body(None)))
        server = request.env["incubacloud.mcp.server"]
        return respond(server._handle(message, httprequest.headers, principal))

    def _note_failure(self, Auth, ip, reliable):
        """Count a failed authentication of *ip*; log it when over the limit
        and the address cannot be refused.

        :param Auth: the ``incubacloud.mcp.auth`` model, as the superuser
        :param str ip: the client's address, as :func:`client_ip` reads it
        :param bool reliable: whether the installation declared its proxies,
            so that *ip* is the client's own
        """
        Auth._count_failure(ip)
        if not reliable and Auth._auth_blocked(ip):
            _logger.warning(
                "[mcp] %s failed to authenticate %s times or more in a minute "
                "and is not refused: no trusted proxies are declared, so the "
                "address may be a proxy's that every client shares",
                ip,
                AUTH_FAILURES_PER_MIN,
            )
