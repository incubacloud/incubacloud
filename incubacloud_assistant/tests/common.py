"""Shared fixtures of the assistant's tests: users, settings, conversations."""

import json

from odoo.addons.incubacloud_mcp.tests.common import McpUsersMixin, PanelFixtureMixin

from ..models.cloud_settings import BLOCKED_PARAM

#: The provider key of the tests: a secret, as a real one.
KEY = "sk-ant-assistant-test-key-0001"
#: The models the provider lists in the tests.
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")


def listing(ids=MODELS, tools=True, price_in=None, price_out=None):
    """Return a provider's listing of *ids*, as the settings keep it."""
    return [
        {
            "id": model,
            "name": model,
            "tools": tools,
            "price_in": price_in,
            "price_out": price_out,
        }
        for model in ids
    ]


class AssistantMixin(McpUsersMixin, PanelFixtureMixin):
    """The panel's assistant switched on for some users."""

    def _configure(
        self,
        users=(),
        provider="anthropic",
        models=MODELS,
        allowed=MODELS[:2],
        default=MODELS[0],
        limit=0.0,
        step_cap=30,
        enabled=True,
        key=KEY,
    ):
        """Switch the assistant on for *users*; return the settings.

        The parameter that blocks it (IncubaCloud's own panel) goes: these
        tests try the module itself, in every mode its CI installs it.
        """
        self.env["ir.config_parameter"].sudo().set_param(BLOCKED_PARAM, False)
        settings = self.env["cloud.settings"]._get_system()
        settings.write(
            {
                "assistant_enabled": enabled,
                "assistant_provider": provider,
                "assistant_api_key": key,
                "assistant_models": json.dumps(listing(models)),
                "assistant_allowed_models": json.dumps(list(allowed)),
                "assistant_default_model": default,
                "assistant_user_ids": [(6, 0, [user.id for user in users])],
                "assistant_monthly_limit": limit,
                "assistant_step_cap": step_cap,
            }
        )
        # The secret filter reads the ciphertexts with SQL: they must be there.
        self.env.flush_all()
        return settings
