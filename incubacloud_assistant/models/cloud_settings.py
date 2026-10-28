"""The assistant's settings: provider, key, models, who may use it, limits.

Fields of the settings singleton, edited by a manager in Settings →
Assistant. The key is an encrypted field: it never goes back to the
browser, and the MCP's secret filter masks its value in anything an agent
or the assistant reads, and refuses its name in any tool's arguments.
"""

import json
from dataclasses import dataclass

from odoo import api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.incubacloud.models.encrypted_char import EncryptedChar

from ..providers import PROVIDERS

#: Steps one task may take before it stops and asks, when the setting is not
#: positive.
DEFAULT_STEP_CAP = 30
#: A system parameter that keeps the assistant off on this database for good,
#: its value the reason shown (IncubaCloud's own panel sets it, from saas).
BLOCKED_PARAM = "incubacloud_assistant.blocked"
#: The context key the Settings routes set, with sudo, to write the
#: assistant's fields: the ORM alone (a manager's ``call_kw``) never does.
SETTINGS_WRITE = "incubacloud_assistant_settings_write"
#: The prefix of the assistant's fields of the settings.
_FIELDS_PREFIX = "assistant_"


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
    #: Section 12A of the Terms accepted: without it nobody has the assistant.
    terms_accepted: bool = False

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

    # Section 12A of the Terms, accepted on switching the assistant on (28-oct).
    # The system's: a manager writes the settings through the ORM, never these.
    assistant_terms_accepted_on = fields.Datetime(
        string="AI terms accepted on",
        readonly=True,
        copy=False,
        groups="base.group_system",
        help="When a manager last switched the assistant on and accepted section 12A "
        "(AI features) of the Terms.",
    )
    assistant_terms_version = fields.Char(
        string="AI terms version",
        readonly=True,
        copy=False,
        groups="base.group_system",
        help="The version of the Terms accepted, as IncubaCloud recorded it. Empty on "
        "a panel IncubaCloud does not record it for.",
    )
    assistant_terms_accepted_by = fields.Char(
        string="AI terms accepted by",
        readonly=True,
        copy=False,
        groups="base.group_system",
        help="The manager who accepted them: name and login.",
    )
    assistant_terms_accepted_ip = fields.Char(
        string="AI terms accepted from",
        readonly=True,
        copy=False,
        groups="base.group_system",
        help="The address the acceptance came from.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Refuse the assistant's fields, unless they come through Settings."""
        for vals in vals_list:
            self._assistant_check_write(vals)
        return super().create(vals_list)

    def write(self, vals):
        """Refuse the assistant's fields, unless they come through Settings."""
        self._assistant_check_write(vals)
        return super().write(vals)

    def _assistant_check_write(self, vals):
        """Raise when *vals* changes the assistant outside Settings → Assistant.

        The checks of switching it on (section 12A accepted), of the
        provider and its key, of the models and of the limits live in the
        Settings routes: those write in sudo with :data:`SETTINGS_WRITE`
        in the context; a manager's write through the ORM is refused.

        :param dict vals: the values to write
        :raises AccessError: for a field of the assistant's without both
        """
        if not any(name.startswith(_FIELDS_PREFIX) for name in vals):
            return
        if self.env.su and self.env.context.get(SETTINGS_WRITE):
            return
        raise AccessError(
            self.env._("The assistant's settings change in Settings → Assistant only.")
        )

    @api.model
    def _assistant_settings(self):
        """Return the settings singleton the Settings routes write the assistant to."""
        return self._get_system().with_context(**{SETTINGS_WRITE: True})

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
            terms_accepted=bool(settings.assistant_terms_accepted_on),
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
    def _assistant_terms_acceptance(self):
        """Return the last acceptance of section 12A kept here, or None.

        :return: ``{accepted_on, version, user, ip}``, the date in UTC as Odoo
            writes it
        :rtype: dict | None
        """
        settings = self._get_system()
        if not settings.assistant_terms_accepted_on:
            return None
        return {
            "accepted_on": fields.Datetime.to_string(
                settings.assistant_terms_accepted_on
            ),
            "version": settings.assistant_terms_version or "",
            "user": settings.assistant_terms_accepted_by or "",
            "ip": settings.assistant_terms_accepted_ip or "",
        }

    @api.model
    def _assistant_terms_label(self):
        """Return the box a manager ticks to switch the assistant on, translated.

        Core alone has no Terms of IncubaCloud's to accept: a self-hosted
        panel acknowledges what the assistant does. A tenant panel
        overrides it with section 12A of the Terms (incubacloud_tenant).
        The refusal without the box quotes it.

        :rtype: str
        """
        return self.env._(
            "I understand that what the assistant reads is sent to my AI provider, "
            "and that it can make mistakes"
        )

    @api.model
    def _assistant_terms_url(self):
        """Return the address of the Terms the box accepts, or "" for none.

        Empty on core alone: the tab links nowhere. A tenant panel
        overrides it with the platform's Terms (incubacloud_tenant).

        :rtype: str
        """
        return ""

    @api.model
    def _assistant_record_terms_acceptance(self, acceptance):
        """Record a manager's acceptance of section 12A; return what to keep.

        Called on switching the assistant on, before anything is written. A
        tenant panel overrides it to have IncubaCloud record it on the
        customer's contact first (incubacloud_tenant). Here nothing outside
        the panel knows the Terms: the acceptance stays on the panel, with
        no version.

        :param dict acceptance: ``at`` (UTC datetime), ``ip`` and ``user``
        :return: the same keys and ``v``, the version of the Terms accepted
        :rtype: dict
        :raises UserError: when it could not be recorded; the assistant stays off
        """
        return {**acceptance, "v": ""}

    @api.model
    def _assistant_available_for(self, user):
        """Return whether *user* may use the assistant now.

        On, with a key and a default model, and *user* among those the
        manager chose (an active internal user), and section 12A of the Terms
        accepted when it was switched on; never on a database that blocks it
        (:data:`BLOCKED_PARAM`).
        """
        config = self._assistant_config()
        return bool(
            not self._assistant_blocked_reason()
            and config.enabled
            and config.has_key
            and config.default_model
            and config.terms_accepted
            and user.id in config.user_ids
            and user.active
            and not user.share
        )

    @api.model
    def _assistant_save_models(self, listing):
        """Keep the provider's listing of models, as just read with the key.

        :param list listing: ``{id, name, tools, price_in, price_out}`` dicts
        """
        self._assistant_settings().write(
            {
                "assistant_models": json.dumps(listing),
                "assistant_models_at": fields.Datetime.now(),
            }
        )
