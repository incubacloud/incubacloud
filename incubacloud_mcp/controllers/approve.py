"""The page where a person approves what an agent asked for.

Needs the panel session of the user who made the call (D5: the session
is enough, no fresh sign-in). The GET only shows; Approve and Cancel are
POSTs with Odoo's CSRF token. Never framed, never cached, and its
address — which carries the token — is never sent on as a referrer.
"""

from odoo import fields, http
from odoo.http import request

from ..models.mcp_tools_command import COMMAND_TOOLS

_PAGE_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class McpApprovePage(http.Controller):
    """``/cloud/mcp/approve/<token>``: show an approval, record the decision."""

    @http.route(
        "/cloud/mcp/approve/<string:token>",
        type="http",
        auth="user",
        methods=["GET", "POST"],
        readonly=False,
    )
    def approve(self, token, decision=None, **_kwargs):
        """Show an approval, or record the user's decision on it.

        :param str token: the page's token (attacker-controlled)
        :param str decision: ``approve`` or ``cancel``, read on a POST only
        """
        if not request.env.user._is_internal():
            return request.not_found()
        approval = request.env["incubacloud.mcp.approval"]._by_token(token)
        if not approval:
            return self._page("missing", approval, 404)
        if approval.user_id != request.env.user:
            return self._page("other_user", approval.browse(), 403)
        live = (
            approval.state == "pending" and approval.expires_at > fields.Datetime.now()
        )
        if request.httprequest.method == "POST":
            if decision not in ("approve", "cancel"):
                return self._page("pending" if live else "closed", approval, 400)
            if approval._decide(decision == "approve"):
                return self._page(
                    "approved" if decision == "approve" else "cancelled", approval
                )
            return self._page("closed", approval)
        return self._page("pending" if live else "closed", approval)

    @staticmethod
    def _page(state, approval, status=200):
        """Render the page in *state* with its protective headers.

        :param str state: ``pending``, ``approved``, ``cancelled``,
            ``other_user``, ``missing`` or ``closed``
        :param approval: the approval shown (empty but for ``pending``)
        :param int status: the HTTP status
        """
        message = (approval.message or "") if state == "pending" else ""
        command = ""
        # A command comes last in its approval, quoted on a line of its own:
        # the page shows it in a block of its own.
        if message and approval.tool in COMMAND_TOOLS:
            message, _newline, command = message.rpartition("\n")
        response = request.render(
            "incubacloud_mcp.approval_page",
            {
                "state": state,
                "approval": approval if state == "pending" else approval.browse(),
                "message": message,
                "command": command,
                "csrf_token": request.csrf_token(),
                "action": request.httprequest.path,
            },
        )
        response.status_code = status
        for name, value in _PAGE_HEADERS.items():
            response.headers[name] = value
        return response
