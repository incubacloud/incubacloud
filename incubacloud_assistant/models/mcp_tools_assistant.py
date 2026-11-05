"""Settings → Assistant, as agents' tools: read them, change what is not a secret.

A manager's, as the tab is. What it reads is the tab's own answer, with
the key as ``has_api_key`` and the consumption per user and month. What
it changes is what spends the provider's money — the models allowed, the
default one, the monthly limit, the steps per task — and who may use it,
always with the manager's approval, the change said from what to what.
Two things stay in the panel: the key and the provider (a secret, and a
new provider needs its own key), and switching the assistant on, which
records the manager's acceptance of the terms with their name and
address. Switching it off is offered.
"""

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud_mcp.models.mcp_catalog import tool_policies
from odoo.addons.incubacloud_mcp.models.mcp_tools import quoted
from odoo.addons.incubacloud_mcp.protocol.tool_spec import (
    ToolSpec,
    array,
    boolean,
    integer,
    obj,
    record_id,
    string,
)

_MANAGER = "group_cloud_manager"

#: What ``update_assistant_settings`` may change, as ``save_settings`` names it.
SAVABLE = (
    "enabled",
    "default_model",
    "allowed_models",
    "user_ids",
    "monthly_limit",
    "step_cap",
)

TOOLS = (
    ToolSpec(
        "get_assistant_settings",
        "The panel assistant's settings: whether it is on, its provider and "
        "whether a key is set (has_api_key), the models the provider lists, "
        "the allowed and default ones, its users, monthly limit and steps per "
        "task; and the consumption of each user per month (usage).",
        _MANAGER,
        obj(
            {
                "months": integer(
                    "How many months of consumption; 12 by default.",
                    minimum=1,
                    maximum=24,
                )
            }
        ),
        handler="_tool_get_assistant_settings",
        read_only=True,
    ),
    ToolSpec(
        "update_assistant_settings",
        "Change the panel assistant's settings (only the values given change), "
        "or with action refresh_models read the provider's models again with "
        "the key kept. The user approves each change. Its key and provider are "
        "set in the panel, and so is switching it on (it records the manager's "
        "acceptance of the terms): enabled takes false only.",
        _MANAGER,
        obj(
            {
                "action": string(
                    "What to do; save by default.", enum=("save", "refresh_models")
                ),
                "values": obj(
                    {
                        "enabled": boolean("false switches the assistant off."),
                        "default_model": string(
                            "The default model, one of the allowed ones.",
                            max_length=200,
                        ),
                        "allowed_models": array(
                            "The models allowed, all of them, as the provider "
                            "lists them.",
                            string("A model.", max_length=200),
                        ),
                        "user_ids": array(
                            "The panel's users who may use it, all of them.",
                            record_id("A user (from candidates)."),
                        ),
                        "monthly_limit": {
                            "type": "number",
                            "minimum": 0,
                            "description": "The monthly limit in euros; 0 for none.",
                        },
                        "step_cap": integer(
                            "The steps per task.", minimum=1, maximum=200
                        ),
                    }
                ),
            }
        ),
        handler="_tool_update_assistant_settings",
        confirm="conditional",
    ),
)

ROUTES = {
    "/cloud/assistant/get_settings": "get_assistant_settings",
    "/cloud/assistant/usage": "get_assistant_settings",
    "/cloud/assistant/save_settings": "update_assistant_settings",
    "/cloud/assistant/refresh_models": "update_assistant_settings",
}


class McpCatalogAssistantSettings(models.AbstractModel):
    """The assistant's settings tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the assistant's settings tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the settings routes these tools call."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsAssistantSettings(models.AbstractModel):
    """What the assistant's settings tools read, ask and save."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _tool_get_assistant_settings(self, args):
        """The tab's settings, and the consumption it shows."""
        settings = self._route("/cloud/assistant/get_settings")
        usage = self._route("/cloud/assistant/usage", months=args.get("months") or 12)
        return {**settings, "usage": usage.get("rows", [])}

    @api.model
    def _assistant_values(self, args):
        """Return the values to save, refusing a switch-on.

        :raises UserError: for ``enabled`` true
        """
        values = args.get("values") or {}
        if values.get("enabled"):
            raise UserError(
                self.env._(
                    "Switching the assistant on records your acceptance of its "
                    "terms: do it in the panel, Settings → Assistant."
                )
            )
        return values

    @api.model
    def _confirm_update_assistant_settings(self, args):
        """Say every setting that changes, from what to what; nothing to ask
        for a refresh of the models, or a save that changes nothing."""
        self.env["cloud.security.mixin"]._check_can_manage_settings()
        if (args.get("action") or "save") != "save":
            return None
        values = self._assistant_values(args)
        current = self._route("/cloud/assistant/get_settings")
        changes = [
            (key, current.get(key), value)
            for key, value in sorted(values.items())
            if value != current.get(key)
            and not (
                isinstance(value, list)
                and sorted(value, key=str) == sorted(current.get(key) or [], key=str)
            )
        ]
        if not changes:
            return None
        lines = "\n".join(
            f"- {quoted(key)}: {old!r} → {new!r}" for key, old, new in changes
        )
        return self.env._(
            "Change these settings of the panel's assistant, which decide what it "
            "may spend at the AI provider and who uses it:\n%(changes)s",
            changes=lines,
        )

    @api.model
    def _tool_update_assistant_settings(self, args):
        """Save the values given, or refresh the provider's models."""
        if (args.get("action") or "save") == "refresh_models":
            return self._route("/cloud/assistant/refresh_models")
        values = self._assistant_values(args)
        if not values:
            raise UserError(self.env._("Give the settings to change (values)."))
        return self._route("/cloud/assistant/save_settings", vals=dict(values))
