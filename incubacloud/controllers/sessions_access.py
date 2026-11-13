"""Sessions and access: the page's calls (``models/cloud_session_access.py``).

Every call is about the signed-in user and nobody else, and none asks for
a password or a second factor (the platform owner's decision,
11-nov-2026).
"""
from odoo import http
from odoo.http import request


class SessionsAccess(http.Controller):

    @http.route("/cloud/sessions/get", type="jsonrpc", auth="user", methods=["POST"])
    def cloud_sessions_get(self):
        """Return what "Sessions and access" shows the signed-in user.

        :rtype: dict
        """
        return {"ok": True, **request.env.user._ic_sessions_and_access()}

    @http.route(
        "/cloud/sessions/sign_out_everywhere", type="jsonrpc", auth="user",
        methods=["POST"],
    )
    def cloud_sessions_sign_out_everywhere(self):
        """Sign the user out of every session, this one included.

        Refused where the page offers a link instead of the button: there
        the sessions are closed from another database.

        :return: ``ok`` and where to send the browser
        :rtype: dict
        """
        user = request.env.user
        if user._ic_everywhere().get("mode") != "button":
            return {"ok": False}
        user._ic_sign_out_everywhere()
        request.session.logout(keep_db=True)
        return {"ok": True, "redirect": "/web/login"}

    @http.route("/cloud/sessions/revoke", type="jsonrpc", auth="user", methods=["POST"])
    def cloud_sessions_revoke(self, kind, door_id):
        """Revoke one of the user's ways in.

        :param str kind: the way in's kind, as the page listed it
        :param int door_id: its id
        :return: ``ok``, and the page's data afresh when it was revoked
        :rtype: dict
        """
        user = request.env.user
        try:
            door_id = int(door_id)
        except (TypeError, ValueError):
            return {"ok": False}
        if not user._ic_revoke_door(str(kind or ""), door_id):
            return {"ok": False}
        return {"ok": True, **user._ic_sessions_and_access()}
