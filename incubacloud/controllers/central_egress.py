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
from ._rate_limit import Rule, first_tripped

#: Asks per address and minute: one per agents' install, which runs a few
#: times an hour at most.
_PER_MINUTE = 30


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
        the caller nothing it does not know already.

        :return: the address, or 429 when asked too often.
        """
        address = client_ip()
        if first_tripped(Rule(
            f"egress_ip:{address}",
            max_per_window=_PER_MINUTE,
            log_tag=f"egress_ip address={address}",
        )):
            return request.make_response(
                "Too many requests", status=429,
                headers=[("Content-Type", "text/plain; charset=utf-8")],
            )
        return request.make_response(
            address,
            headers=[
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Cache-Control", "no-store"),
            ],
        )
