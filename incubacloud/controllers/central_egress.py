"""Tell a server which address its traffic leaves with.

The central's single name admits only the panel's servers by their
address. A server behind NAT, or whose provider routes it out through
another address, is refused there although the panel has its address
right for SSH; the agents' install asks here which address the panel
sees it with, and the alert that the server is outside names it
(``cloud.host._record_central_reach``).
"""
from odoo import http
from odoo.http import request

from ._client_ip import client_ip


class CentralEgressController(http.Controller):
    """The one unauthenticated answer the agents' install asks the panel."""

    @http.route(
        "/cloud/egress_ip", type="http", auth="none", methods=["GET"],
        csrf=False, save_session=False,
    )
    def egress_ip(self):
        """Return the caller's address as plain text.

        Seen through the proxies the installation declares
        (``_client_ip``), as every rate limit here sees it. It says to
        the caller nothing it does not know already, and costs less than
        a rate limit would: a limit per address wrote a row for every
        address that asked, and IPv6 gives a caller as many as it wants.
        Nothing is written and no session is opened.

        :return: the address.
        """
        return request.make_response(
            client_ip(),
            headers=[
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Cache-Control", "no-store"),
            ],
        )
