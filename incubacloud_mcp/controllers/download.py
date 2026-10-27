"""``/cloud/mcp/download/<token>``: the file a download link names, once."""

from odoo import http
from odoo.http import request

#: Headers of the file: never cached, its address never sent on.
_FILE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class McpDownloadLink(http.Controller):
    """Serve the backups agents' links name."""

    @http.route(
        "/cloud/mcp/download/<string:token>",
        type="http",
        auth="public",
        methods=["GET"],
        save_session=False,
        readonly=False,
    )
    def download(self, token, **_kwargs):
        """Serve the backup of a live, unused link; 404 otherwise.

        A HEAD (which the routing accepts with a GET) is refused without
        spending the link: link previews send one.

        :param str token: the link's token (attacker-controlled)
        """
        if request.httprequest.method != "GET":
            return request.make_response("", headers=[("Allow", "GET")], status=405)
        grant = (
            request.env["incubacloud.mcp.download"]
            .sudo()
            ._redeem(token, request.session.uid)
        )
        if not grant:
            return request.not_found()
        response = (
            request.env["ir.binary"]
            .sudo()
            ._get_stream_from(grant.attachment_id)
            .get_response(as_attachment=True)
        )
        for name, value in _FILE_HEADERS.items():
            response.headers[name] = value
        return response
