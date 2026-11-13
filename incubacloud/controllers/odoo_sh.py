"""Odoo.sh's notice that the panel's outgoing address changed.

Odoo.sh's documentation (Advanced › IP address change): when the address
of a production instance changes, the platform makes an HTTP ``GET`` to
``/_odoo.sh/ip-change`` with the new address in ``new`` and the previous
one in ``old``; the path is reachable only from the platform itself and
answers 403 to anyone else. It does not say whether the request comes
before or after the change, whether it is retried, or what it does with
the answer.

Only the platform's proxy keeps others out, and only on Odoo.sh: on any
other installation this path is as public as the rest. So the route
answers only where Odoo.sh's production says it runs (``ODOO_STAGE`` is
``production``), and 404 everywhere else, its staging and development
branches included. Even there it changes nothing: it raises an alert
(``cloud.host._panel_address_changed``), which is all a forged request
could get.
"""
import logging
import os

from odoo import http
from odoo.http import request

from ..models.res_users_ext import as_platform

_logger = logging.getLogger(__name__)


class OdooShController(http.Controller):
    """The requests Odoo.sh's platform makes to the database it runs."""

    @http.route(
        "/_odoo.sh/ip-change", type="http", auth="public", methods=["GET"],
        csrf=False, save_session=False,
    )
    def ip_change(self, old=None, new=None, **_params):
        """Take Odoo.sh's notice that the outgoing address changed.

        :param str old: the address the panel left with until now
        :param str new: the address it leaves with now
        :return: ``ok``; 400 when *old* is not an address; 404 off
            Odoo.sh's production
        """
        if os.environ.get("ODOO_STAGE") != "production":
            return request.not_found()
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ]
        Host = as_platform(request.env["cloud.host"])
        try:
            Host._panel_address_changed(old, new)
        except ValueError:
            _logger.warning(
                "Odoo.sh's notice of an address change came without an old address",
            )
            return request.make_response("bad request", headers=headers, status=400)
        return request.make_response("ok", headers=headers)
