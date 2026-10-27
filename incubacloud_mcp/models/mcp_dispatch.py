"""A tool calls the route the panel itself would serve.

The route is looked up in the database's routing map and its final
endpoint is called — the most derived override, the one Odoo would run —
so whatever saas puts on top of a core route applies to agents too. No
logic is copied out of the routes and no core class is imported.

``ir.http.routing_map()`` is read directly, not through ``ir.http._match``:
a tenant panel's ``_match`` redirects any request without a session.
"""

from werkzeug.exceptions import HTTPException

from odoo import api, models
from odoo.exceptions import UserError


class McpDispatch(models.AbstractModel):
    """Calls the panel's JSON-RPC routes for the tools."""

    _name = "incubacloud.mcp.dispatch"
    _description = "Calls panel routes for MCP tools"

    @api.model
    def _call(self, path, params):
        """Call the JSON-RPC route at *path* with *params*; return its answer.

        The caller's ``request.env`` is the user's: the route's own role
        checks and record rules apply as in the panel.

        :raises UserError: when *path* is not a JSON-RPC route here
        """
        adapter = self.env["ir.http"].routing_map().bind("localhost")
        try:
            rule, path_args = adapter.match(path, method="POST", return_rule=True)
        except HTTPException as exc:
            raise UserError(self.env._("The panel has no route %s.", path)) from exc
        if rule.endpoint.routing.get("type") != "jsonrpc":
            raise UserError(self.env._("The panel has no route %s.", path))
        return rule.endpoint(**path_args, **params)
