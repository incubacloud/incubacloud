"""``/cloud/mcp/download/<token>``: the file a download link names, once."""

import logging
import posixpath

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

#: Headers of the file: never cached, its address never sent on.
_FILE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def download_name(name):
    """Return the plain name a file is saved under: no directory, quote or
    control character, never empty.

    :param str name: the attachment's name (it may come from anywhere)
    """
    base = posixpath.basename(str(name or "").replace("\\", "/"))
    base = "".join(char for char in base if char.isprintable() and char != '"')
    return base.strip(". ")[:200] or "backup"


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
        spending the link: link previews send one. A file gone from the
        filestore answers 410; the file is saved under a plain name.

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
        try:
            stream = (
                request.env["ir.binary"].sudo()._get_stream_from(grant.attachment_id)
            )
        except OSError:
            # The file left the filestore after the link was issued.
            _logger.warning(
                "[mcp] the file of download link %s is gone", grant.id, exc_info=True
            )
            return request.make_response("", status=410)
        stream.download_name = download_name(grant.attachment_id.name)
        response = stream.get_response(as_attachment=True)
        for name, value in _FILE_HEADERS.items():
            response.headers[name] = value
        return response
