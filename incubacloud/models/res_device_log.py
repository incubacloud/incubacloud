"""Where the session guard hooks into Odoo's session check.

Odoo's ``check_session`` calls ``res.device.log._update_device`` on every
request with a signed-in session, after the session token matched and
before the request is served, and pays no attention to what it returns.
The guard runs first, while the session's trace still holds the last
activity it is judged on; a close raises ``SessionExpiredException``,
which Odoo turns into the same sign-out as an expired session — on an
HTTP route, a JSON route and the websocket alike. Odoo 18 calls the same
method from the same place.
"""
from odoo import api, models


class ResDeviceLog(models.Model):
    _inherit = "res.device.log"

    @api.model
    def _update_device(self, request):
        """Judge the session (``cloud.session.guard``), then trace it.

        :param request: the request or websocket request being
            authenticated
        :raises SessionExpiredException: when the guard closed the session
        """
        self.env["cloud.session.guard"]._session_guard_check(request)
        return super()._update_device(request)
