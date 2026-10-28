"""The assistant's routes are the panel's own: no agent's tool calls them."""

from odoo import api, models

_CHAT = "The assistant's chat, for the person in the panel."
_SETTINGS = "The assistant's settings, for a manager in the panel."

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
    **{
        f"/cloud/assistant/{name}": _SETTINGS
        for name in ("get_settings", "save_settings", "refresh_models", "usage")
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
