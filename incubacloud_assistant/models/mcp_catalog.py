"""The assistant's chat is the panel's own: no agent's tool calls its routes.

Its settings are read and changed through ``mcp_tools_assistant``, but for
the trial of a new key.
"""

from odoo import api, models

_CHAT = "The assistant's chat, for the person in the panel."
_KEY = (
    "Tries a provider's key typed in the panel: a secret, which agents never "
    "send (decision 6)."
)

ASSISTANT_EXCLUDED = {
    **{
        f"/cloud/assistant/{name}": _CHAT
        for name in (
            "status",
            "accept_notice",
            "conversations",
            "conversation",
            "send",
            "set_model",
            "stop",
            "retry",
            "confirm",
            "cancel",
            "delete",
        )
    },
    "/cloud/assistant/read_models": _KEY,
}


#: The chat calls these through a variable (``_call(route)``, ``rpc(route)``):
#: the routes it takes, for the inventory test.
DYNAMIC_CALLS = {
    "/incubacloud_assistant/static/src/panel/assistant_panel.js": {
        "route": frozenset(
            {
                "/cloud/assistant/stop",
                "/cloud/assistant/retry",
                "/cloud/assistant/confirm",
                "/cloud/assistant/cancel",
            }
        ),
    },
}


class McpCatalogAssistant(models.AbstractModel):
    """Classify the assistant's routes for the MCP's inventory."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _route_policies(self):
        """Exclude every route of the assistant, with its reason."""
        return {
            **super()._route_policies(),
            **{
                route: ("excluded", reason)
                for route, reason in ASSISTANT_EXCLUDED.items()
            },
        }

    @api.model
    def _dynamic_calls(self):
        """Declare the chat's calls through a variable route."""
        return {**super()._dynamic_calls(), **DYNAMIC_CALLS}
