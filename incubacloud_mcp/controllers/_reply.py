"""The one way the MCP controllers answer: JSON, never cached by default."""

import json

from odoo.http import request


def respond(reply):
    """Turn a :class:`~..protocol.context.Reply` into the HTTP response.

    :param reply: status, JSON body (or None for an empty body), headers
    """
    headers = dict(reply.headers)
    headers.setdefault("Cache-Control", "no-store")
    body = ""
    if reply.body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(reply.body, default=str)
    return request.make_response(
        body, headers=list(headers.items()), status=reply.status
    )
