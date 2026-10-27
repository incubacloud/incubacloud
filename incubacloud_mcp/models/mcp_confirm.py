"""Approval of agents' destructive and security-sensitive actions."""

from odoo import api, models


class McpConfirm(models.AbstractModel):
    """Whether a call may run now, or waits for the user's approval."""

    _name = "incubacloud.mcp.confirm"
    _description = "Approval of agents' sensitive actions"

    @api.model
    def _gate(self, spec, args, ctx, params):
        """Return None when the call may run now, or the result that stops it.

        Fails closed: a call that needs an approval is refused until the
        approval flow (``incubacloud.mcp.approval``) is in place.

        :param spec: the tool's :class:`ToolSpec`
        :param dict args: the validated arguments
        :param ctx: the request's :class:`CallContext`
        :param dict params: the ``tools/call`` params, as the client sent them
        """
        if spec.confirm == "never":
            return None
        if not self.env["incubacloud.mcp.tools"]._confirmation(spec, args):
            return None
        return self.env["incubacloud.mcp.server"]._error_result(
            ctx,
            self.env._(
                "This action needs your approval, which this panel cannot ask for yet."
            ),
        )
