"""Where the session guard hooks into Odoo's session check.

Odoo's ``check_session`` calls ``res.device.log._update_device`` on every
request with a signed-in session, after the session token matched and
before the request is served, and pays no attention to what it returns.
The guard runs first, while the session's trace still holds the last
activity it is judged on; a close raises ``SessionExpiredException``,
which Odoo turns into the same sign-out as an expired session — on an
HTTP route, a JSON route and the websocket alike. Odoo 18 calls the same
method from the same place.

Any other failure of the guard is logged and the request goes on: Odoo
turns whatever ``check_session`` raises into a 403 on every route, the
sign-in and the sign-out included (review of 12-nov-2026, A2).

On a route open to visitors (``auth='public'`` or ``'none'``) a close is
served as Odoo serves a session it no longer accepts there: signed out,
as a visitor, instead of sent to the sign-in (M1 of the same review).
"""
import logging

from odoo import api, http, models
from odoo.http import SessionExpiredException

from .cloud_session_guard import CLOSED_MARK

_logger = logging.getLogger(__name__)

#: The routes a closed session is served on as a visitor.
VISITOR_AUTHS = ("public", "none")


class ResDeviceLog(models.Model):
    _inherit = "res.device.log"

    @api.model
    def _update_device(self, request):
        """Judge the session (``cloud.session.guard``), then trace it.

        A failure of the guard other than a close is logged, the
        request's transaction rolled back as the guard's own writes
        already do, and the request goes on: a fault of ours closes
        nobody and blocks nobody.

        :param request: the request or websocket request being
            authenticated
        :raises SessionExpiredException: when the guard closed the session
        """
        try:
            self.env["cloud.session.guard"]._session_guard_check(request)
        except SessionExpiredException:
            raise
        except Exception:  # noqa: BLE001 - never a 403 of ours
            _logger.exception(
                "[session-guard] the guard failed on a request of user %s; "
                "the request goes on", request.session.uid,
            )
            self.env.cr.rollback()
        return super()._update_device(request)


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _authenticate_explicit(cls, auth):
        """Serve a session the guard closed as a visitor, on a route open
        to visitors.

        Odoo does so with a session whose token no longer matches; a
        close raises instead, which on any HTTP route sends the browser
        to the sign-in, so the home or pricing page of somebody whose
        session closed would land them there. The session is already
        destroyed; it is signed out in place and the route's own
        authentication runs as for anybody unknown.

        :param str auth: the route's ``auth``
        :raises SessionExpiredException: on any other route
        """
        try:
            return super()._authenticate_explicit(auth)
        except SessionExpiredException as error:
            if auth not in VISITOR_AUTHS or not getattr(error, CLOSED_MARK, False):
                raise
        request = http.request
        request.session.logout(keep_db=True)
        request.env = api.Environment(
            request.env.cr, None, request.session.context,
        )
        return getattr(cls, f"_auth_method_{auth}")()
