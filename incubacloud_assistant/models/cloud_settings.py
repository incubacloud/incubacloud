"""The assistant's settings: provider, key, models, who may use it, limits.

Fields of the settings singleton, edited by a manager in Settings →
Assistant. The key is an encrypted field: it never goes back to the
browser, and the MCP's secret filter masks its value in anything an agent
or the assistant reads, and refuses its name in any tool's arguments.
"""

import json
from dataclasses import dataclass

from odoo import api, fields, models

from odoo.addons.incubacloud.models.encrypted_char import EncryptedChar

from ..providers import PROVIDERS

#: Steps one task may take before it stops and asks, when the setting is not
#: positive.
DEFAULT_STEP_CAP = 30
#: A system parameter that keeps the assistant off on this database for good,
#: its value the reason shown (IncubaCloud's own panel sets it, from saas).
BLOCKED_PARAM = "incubacloud_assistant.blocked"


def _json_list(text):
    """Return the JSON list in *text*, or ``[]`` when it is not one."""
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class AssistantConfig:
    """The assistant's settings, read once for a step or a request."""

    enabled: bool
    provider: str
    has_key: bool
    default_model: str
    allowed_models: tuple
    #: The provider's listing: ``{id, name, tools, price_in, price_out}`` each.
    models: tuple
    user_ids: frozenset
    #: Euros per month, the same for every user; 0 is no limit.
    monthly_limit: float
    step_cap: int

    def listing(self, model_id):
        """Return the listing's entry of *model_id*, or None."""
        return next((item for item in self.models if item.get("id") == model_id), None)


class CloudSettings(models.Model):
    """The assistant's part of the settings singleton."""

    _inherit = "cloud.settings"

    assistant_enabled = fields.Boolean(
        string="Assistant on",
        help="The assistant shows, for the users chosen, only while this is on.",
    )
    assistant_provider = fields.Selection(
        [(code, label) for code, label, _beta in PROVIDERS],
        string="AI provider",
        default="anthropic",
    )
    assistant_api_key = EncryptedChar(
        string="AI provider key",
        # Not the managers' group: they read the settings through the ORM, and
        # the key never goes back to a browser. The server reads it in sudo.
        groups="base.group_system",
        help="The provider's API key. Stored encrypted; never shown again.",
    )
    assistant_default_model = fields.Char(string="Default model")
    assistant_allowed_models = fields.Text(
        string="Allowed models", default="[]", help="JSON list of model ids."
    )
    assistant_models = fields.Text(
        string="Provider's models",
        default="[]",
        help="The provider's listing as last read with the key (JSON).",
    )
    assistant_models_at = fields.Datetime(string="Models read at")
    assistant_user_ids = fields.Many2many(
        "res.users",
        "cloud_settings_assistant_user_rel",
        "settings_id",
        "user_id",
        string="Users of the assistant",
    )
    assistant_monthly_limit = fields.Float(
        string="Monthly limit per user (EUR)",
        digits=(16, 2),
        help="The same for every user: what each one's conversations may cost per "
        "month, approximately. Zero: no limit. With a limit, models whose price is "
        "not known cannot be used.",
    )
    assistant_step_cap = fields.Integer(
        string="Steps per task",
        default=DEFAULT_STEP_CAP,
        help="Calls to the model one task may make before it stops and asks "
        "whether to go on. Zero or less: 30.",
    )

    @api.model
    def _assistant_config(self):
        """Return the assistant's settings.

        :rtype: AssistantConfig
        """
        settings = self._get_system()
        return AssistantConfig(
            enabled=bool(settings.assistant_enabled),
            provider=settings.assistant_provider or "anthropic",
            has_key=bool((settings.assistant_api_key or "").strip()),
            default_model=settings.assistant_default_model or "",
            allowed_models=tuple(
                str(item) for item in _json_list(settings.assistant_allowed_models)
            ),
            models=tuple(
                item
                for item in _json_list(settings.assistant_models)
                if isinstance(item, dict) and item.get("id")
            ),
            user_ids=frozenset(settings.assistant_user_ids.ids),
            monthly_limit=max(0.0, settings.assistant_monthly_limit or 0.0),
            step_cap=settings.assistant_step_cap
            if settings.assistant_step_cap > 0
            else DEFAULT_STEP_CAP,
        )

    @api.model
    def _assistant_key(self):
        """Return the provider key, for the server's own calls only."""
        return (self._get_system().assistant_api_key or "").strip()

    @api.model
    def _assistant_credentials(self):
        """Return ``(provider, key)`` read together, for the server's own calls.

        One read of the singleton: a key is never sent to a provider it
        was not saved for.
        """
        settings = self._get_system()
        return (
            settings.assistant_provider or "anthropic",
            (settings.assistant_api_key or "").strip(),
        )

    @api.model
    def _assistant_blocked_reason(self):
        """Return why the assistant can never be switched on here, or ""."""
        return self.env["ir.config_parameter"].sudo().get_param(BLOCKED_PARAM) or ""

    @api.model
    def _assistant_available_for(self, user):
        """Return whether *user* may use the assistant now.

        On, with a key and a default model, and *user* among those the
        manager chose (an active internal user); never on a database that
        blocks it (:data:`BLOCKED_PARAM`).
        """
        config = self._assistant_config()
        return bool(
            not self._assistant_blocked_reason()
            and config.enabled
            and config.has_key
            and config.default_model
            and user.id in config.user_ids
            and user.active
            and not user.share
        )

    @api.model
    def _assistant_save_models(self, listing):
        """Keep the provider's listing of models, as just read with the key.

        :param list listing: ``{id, name, tools, price_in, price_out}`` dicts
        """
        self._get_system().write(
            {
                "assistant_models": json.dumps(listing),
                "assistant_models_at": fields.Datetime.now(),
            }
        )
