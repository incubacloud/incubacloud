"""Settings → Assistant: what a manager reads and saves.

The key is write-only: it is tried (the provider's listing of models)
before it is kept, it is never sent back, and an error never quotes it. A
panel without access to the provider learns it here, on save.
"""

import json
import math

from odoo import _, fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.incubacloud.controllers._client_ip import client_ip
from odoo.addons.incubacloud.models.cloud_security_mixin import _GROUP_HIERARCHY

from .. import pricing
from ..connectors import connector_for
from ..connectors.base import ProviderError
from ..providers import PROVIDERS, RECOMMENDED, provider_label

#: What ``save_settings`` accepts.
_SAVABLE = frozenset(
    {
        "enabled",
        "provider",
        "api_key",
        "clear_api_key",
        "default_model",
        "allowed_models",
        "user_ids",
        "monthly_limit",
        "step_cap",
        "accept_ai_terms",
    }
)
#: The steps per task a manager may set.
_STEP_CAP_RANGE = (1, 200)


def _refused(error):
    """Return ``{ok: false, error}``."""
    return {"ok": False, "error": error}


class _Refusal(Exception):
    """A value of the save that is refused; its text is what the manager reads."""


def _number(value, cast, fallback):
    """Return *value* as *cast* (``int``, ``float``), or *fallback* if it is none."""
    try:
        return cast(value)
    except (TypeError, ValueError):
        return fallback


class AssistantSettingsRoutes(http.Controller):
    """Routes of Settings → Assistant. Managers."""

    @staticmethod
    def _check():
        """Raise unless the signed-in person manages the panel's settings."""
        request.env["cloud.security.mixin"]._check_can_manage_settings()

    @staticmethod
    def _role(user):
        """Return the highest cloud role of *user* (its group's name)."""
        return next(
            (
                group
                for group in reversed(_GROUP_HIERARCHY)
                if user.has_group(f"incubacloud.{group}")
            ),
            "",
        )

    def _candidates(self):
        """Return the panel's internal users with a cloud role.

        The platform's own users are left out
        (``cloud.settings._assistant_is_platform_user``): the cron bot and
        Odoo's, and whatever support user a module that extends the panel
        adds. The manager
        sees, and may choose, the people of the panel only.
        """
        Settings = request.env["cloud.settings"]
        groups = [
            request.env.ref(f"incubacloud.{group}").id for group in _GROUP_HIERARCHY
        ]
        users = (
            request.env["res.users"]
            .sudo()
            .search(
                [
                    ("share", "=", False),
                    ("active", "=", True),
                    ("all_group_ids", "in", groups),
                ],
                order="name",
            )
        )
        return [
            {
                "id": user.id,
                "name": user.name,
                "login": user.login,
                "role": self._role(user),
            }
            for user in users
            if not Settings._assistant_is_platform_user(user)
        ]

    @staticmethod
    def _models(config):
        """Return the provider's listing kept in *config*, tagged."""
        return AssistantSettingsRoutes._tagged(config.provider, config.models)

    @staticmethod
    def _tagged(provider, items):
        """Return *provider*'s listing *items*, each tagged as the tab shows it.

        :param str provider: the provider's code
        :param items: ``{id, name, tools, price_in, price_out}`` dicts
        :return: ``{id, name, tools, recommended, price_known}`` dicts
        :rtype: list
        """
        recommended = RECOMMENDED.get(provider, ())
        by_id = {item["id"]: item for item in items}
        return [
            {
                "id": item["id"],
                "name": item.get("name") or item["id"],
                "tools": item.get("tools"),
                "recommended": item["id"] in recommended,
                "price_known": pricing.price_for(provider, item["id"], by_id.get)
                is not None,
            }
            for item in items
        ]

    @staticmethod
    def _listing(provider, key):
        """Return ``(models, None)`` read with *key*, or ``(None, why not)``.

        Models that say they take no tools are left out.
        """
        label = provider_label(provider)
        try:
            models = connector_for(provider, key).list_models()
        except ProviderError as error:
            texts = {
                "invalid_key": _("%s refused this key.", label),
                "quota": _("%s says the account has no credit left.", label),
                "unreachable": _(
                    "The panel could not reach %s. Check that this server can open "
                    "the provider's address: a panel without internet access cannot "
                    "use the assistant.",
                    label,
                ),
                "unavailable": _("%s is not answering. Try again in a moment.", label),
                "rate_limited": _("%s is busy. Try again in a moment.", label),
            }
            text = texts.get(error.kind) or _(
                "%(provider)s answered: %(message)s",
                provider=label,
                message=(error.message or "").replace(key, "***")[:300],
            )
            return None, text
        return [model.as_dict() for model in models if model.tools is not False], None

    @http.route("/cloud/assistant/get_settings", type="jsonrpc", auth="user")
    def assistant_get_settings(self):
        """The assistant's settings, the provider's models, the panel's users."""
        self._check()
        Settings = request.env["cloud.settings"]
        config = Settings._assistant_config()
        candidates = self._candidates()
        return {
            "enabled": config.enabled,
            "provider": config.provider,
            "providers": [
                {"value": code, "label": label, "beta": beta}
                for code, label, beta in PROVIDERS
            ],
            "has_api_key": config.has_key,
            "default_model": config.default_model,
            "allowed_models": list(config.allowed_models),
            "models": self._models(config),
            "models_at": fields.Datetime.to_string(
                Settings._get_system().assistant_models_at
            )
            or "",
            # Among the candidates only: a user chosen before the platform's
            # were left out is not sent back with the next save.
            "user_ids": sorted(config.user_ids & {c["id"] for c in candidates}),
            "candidates": candidates,
            "monthly_limit": config.monthly_limit,
            "step_cap": config.step_cap,
            "blocked": Settings._assistant_blocked_reason(),
            "terms_acceptance": Settings._assistant_terms_acceptance(),
            "terms_label": Settings._assistant_terms_label(),
            "terms_url": Settings._assistant_terms_url(),
        }

    @http.route("/cloud/assistant/save_settings", type="jsonrpc", auth="user")
    def assistant_save_settings(self, vals):
        """Save what *vals* holds, all of it checked; a new key is tried first.

        A new provider comes with its own key, typed in the same save: the
        key of one provider is never sent to another. It starts with no
        model allowed: its listing is another. Switching on needs a key, a
        default model and allowed models. With a monthly limit, the default
        model must have a known price. A new provider shows everyone the
        first-use notice again: it names the provider. It also switches the
        assistant off: switching it on again asks for the box, as any
        switch-on.

        Switching on with a key but without allowed models or a default
        model is not refused: the rest of the save is kept (a new key and
        its listing with it), the assistant stays off, and ``warning`` says
        what is missing.

        :return: ``{ok: true}``, with ``warning`` when it was not switched
            on; or ``{ok: false, error}`` and nothing saved
        """
        self._check()
        try:
            write, warning = self._changes(dict(vals or {}))
        except _Refusal as refusal:
            return _refused(str(refusal))
        if write:
            request.env["cloud.settings"]._assistant_settings().write(write)
        if "assistant_provider" in write:
            # The notice names the provider: everyone reads it again.
            request.env["res.users"].sudo().with_context(active_test=False).search(
                [("assistant_notice_at", "!=", False)]
            ).write({"assistant_notice_at": False})
        if warning:
            return {"ok": True, "warning": warning}
        return {"ok": True}

    def _changes(self, vals):
        """Return ``(fields, warning)``: the fields *vals* changes, every one checked.

        *warning* is why the assistant was not switched on, or "".

        :raises _Refusal: with the reason, for the first value refused
        """
        unknown = sorted(set(vals) - _SAVABLE)
        if unknown:
            raise _Refusal(_("Unknown setting(s): %s", ", ".join(unknown)))
        Settings = request.env["cloud.settings"]
        config = Settings._assistant_config()
        provider = vals.get("provider", config.provider)
        key = str(vals.get("api_key") or "").strip()
        write, items = self._provider_and_key(vals, config, provider, key)
        allowed, default = self._models_chosen(vals, config, items, write)
        self._users_and_limits(vals, write)
        limit = write.get("assistant_monthly_limit", config.monthly_limit)
        self._check_default_price(provider, default, items, limit)
        warning = self._switch(vals, key, allowed, default, write)
        return write, warning

    def _provider_and_key(self, vals, config, provider, key):
        """Return ``(fields, the provider's listing)`` for the provider and the key.

        A new provider has no model allowed yet: the assistant goes off.

        :raises _Refusal: for an unknown provider, a new provider without
            its key, or a key the provider refuses
        """
        if provider not in [code for code, _label, _beta in PROVIDERS]:
            raise _Refusal(_("Unknown AI provider."))
        changed = provider != config.provider
        clearing = bool(vals.get("clear_api_key")) and not key
        if changed and not key and not clearing:
            raise _Refusal(
                _(
                    "Type the new provider's key with it: the panel never sends one "
                    "provider's key to another."
                )
            )
        write, items = {}, list(config.models)
        if changed:
            write.update(
                assistant_provider=provider,
                assistant_models="[]",
                assistant_models_at=False,
                assistant_allowed_models="[]",
                assistant_default_model=False,
                assistant_enabled=False,
            )
            items = []
        if clearing:
            write.update(assistant_api_key=False, assistant_enabled=False)
        elif key:
            listing, error = self._listing(provider, key)
            if error:
                raise _Refusal(error)
            write.update(
                assistant_api_key=key,
                assistant_models=json.dumps(listing),
                assistant_models_at=fields.Datetime.now(),
            )
            items = listing
        return write, items

    @staticmethod
    def _models_chosen(vals, config, items, write):
        """Return ``(allowed, default)`` once *vals* applies, checked on the listing.

        :raises _Refusal: for a model the provider does not list, or a
            default outside the allowed ones
        """
        changed = "assistant_provider" in write
        allowed = [] if changed else list(config.allowed_models)
        default = "" if changed else config.default_model
        if "allowed_models" in vals:
            allowed = vals["allowed_models"]
            known = {item["id"] for item in items}
            if (
                not isinstance(allowed, list)
                or not all(isinstance(model, str) for model in allowed)
                or set(allowed) - known
            ):
                raise _Refusal(_("Allow only models the provider lists."))
            write["assistant_allowed_models"] = json.dumps(allowed)
        if "default_model" in vals:
            default = vals["default_model"] or ""
            if default and default not in allowed:
                raise _Refusal(
                    _("The default model must be one of the allowed models.")
                )
            write["assistant_default_model"] = default or False
        return allowed, default

    def _users_and_limits(self, vals, write):
        """Check the users, the monthly limit and the steps per task of *vals*.

        :raises _Refusal: for a user without a role, or a value out of range
        """
        if "user_ids" in vals:
            ids = vals["user_ids"]
            candidates = {candidate["id"] for candidate in self._candidates()}
            if not isinstance(ids, list) or set(ids) - candidates:
                raise _Refusal(_("Choose users of the panel with a role."))
            write["assistant_user_ids"] = [(6, 0, ids)]
        if "monthly_limit" in vals:
            limit = _number(vals["monthly_limit"] or 0, float, -1)
            # NaN passes "< 0" and no spending ever reaches it.
            if not math.isfinite(limit) or limit < 0:
                raise _Refusal(_("The monthly limit is a number of euros, 0 or more."))
            write["assistant_monthly_limit"] = limit
        if "step_cap" in vals:
            cap = _number(vals["step_cap"], int, 0)
            if not _STEP_CAP_RANGE[0] <= cap <= _STEP_CAP_RANGE[1]:
                raise _Refusal(_("Steps per task go from 1 to 200."))
            write["assistant_step_cap"] = cap

    @staticmethod
    def _check_default_price(provider, default, items, limit):
        """Refuse a monthly limit while the default model's price is unknown.

        :raises _Refusal: saying to pick a priced default or drop the limit
        """
        by_id = {item["id"]: item for item in items}
        unpriced = pricing.price_for(provider, default, by_id.get) is None
        if limit and default and unpriced:
            raise _Refusal(
                _(
                    "The default model has no known price, and a monthly limit is set: "
                    "the assistant could not count what it costs. Pick a default model "
                    "with a known price, or set the limit to 0."
                )
            )

    @staticmethod
    def _switch(vals, key, allowed, default, write):
        """Check switching the assistant on or off with what the save leaves.

        Switching it on (it was off, or the save changes the provider) needs
        the box ticked in the same save, recorded last of all
        (:meth:`_terms_accepted`). With a key but without allowed models or
        a default model, it stays off and the rest of the save goes on: a
        key typed in it was tried, and is kept with its listing, so the
        models can be chosen from it.

        :return: why it was not switched on, or ""
        :rtype: str
        :raises _Refusal: on a database that blocks it, without a key, or
            without the acceptance
        """
        if "enabled" not in vals or write.get("assistant_api_key") is False:
            return ""
        Settings = request.env["cloud.settings"]
        blocked = Settings._assistant_blocked_reason()
        if vals["enabled"] and blocked:
            raise _Refusal(blocked)
        has_key = bool(key or Settings._assistant_key())
        if vals["enabled"] and not has_key:
            raise _Refusal(
                _(
                    "Set the provider's key, the allowed models and a default "
                    "model before switching it on."
                )
            )
        if vals["enabled"] and not (default and allowed):
            write["assistant_enabled"] = False
            if key:
                return _(
                    "Key saved. Choose the allowed models and a default model, "
                    "then switch the assistant on."
                )
            return _(
                "Choose the allowed models and a default model, then switch the "
                "assistant on."
            )
        was_on = Settings._assistant_config().enabled
        if vals["enabled"] and (not was_on or "assistant_provider" in write):
            write.update(AssistantSettingsRoutes._terms_accepted(vals))
        write["assistant_enabled"] = bool(vals["enabled"])
        return ""

    @staticmethod
    def _terms_accepted(vals):
        """Record the manager's ticked box; return its fields.

        The box (``accept_ai_terms``) must be ``True`` itself: a section
        of the Terms where a layer has them, an acknowledgement on core alone
        (``cloud.settings._assistant_terms_label``). The hook records it (where
        a layer has Terms, there first, which answers the version); a
        refusal of the hook is the save's.

        :raises _Refusal: without the box, or when it could not be recorded
        """
        Settings = request.env["cloud.settings"]
        if vals.get("accept_ai_terms") is not True:
            raise _Refusal(
                _(
                    'Tick "%s" to switch the assistant on.',
                    Settings._assistant_terms_label(),
                )
            )
        user = request.env.user
        try:
            kept = Settings._assistant_record_terms_acceptance(
                {
                    "at": fields.Datetime.now(),
                    "ip": client_ip(),
                    "user": f"{user.name} ({user.login})",
                }
            )
        except UserError as error:
            raise _Refusal(error.args[0]) from None
        return {
            "assistant_terms_accepted_on": kept["at"],
            "assistant_terms_version": kept["v"] or False,
            "assistant_terms_accepted_by": kept["user"],
            "assistant_terms_accepted_ip": kept["ip"],
        }

    @http.route("/cloud/assistant/refresh_models", type="jsonrpc", auth="user")
    def assistant_refresh_models(self):
        """Read the provider's models again with the key kept."""
        self._check()
        Settings = request.env["cloud.settings"]
        key = Settings._assistant_key()
        if not key:
            return _refused(_("Set the provider's key first."))
        config = Settings._assistant_config()
        listing, error = self._listing(config.provider, key)
        if error:
            return _refused(error)
        Settings._assistant_save_models(listing)
        return {"ok": True, "models": self._models(Settings._assistant_config())}

    @http.route("/cloud/assistant/read_models", type="jsonrpc", auth="user")
    def assistant_read_models(self, provider=None, api_key=None):
        """Read the provider's models with a key typed in the form; keep nothing.

        The tab lists them to choose from before anything is saved: neither
        the key nor the listing is kept here. Save tries the key again and
        keeps both (:meth:`assistant_save_settings`). The key goes to the
        provider it was typed for only, and an error never quotes it.

        :param str provider: the provider of the form; the saved one if none
        :param str api_key: the key typed
        :return: ``{ok: true, models}``, tagged as ``get_settings`` tags them,
            or ``{ok: false, error}``
        """
        self._check()
        provider = provider or request.env["cloud.settings"]._assistant_config().provider
        if provider not in [code for code, _label, _beta in PROVIDERS]:
            return _refused(_("Unknown AI provider."))
        key = str(api_key or "").strip()
        if not key:
            return _refused(_("Type the provider's key first."))
        listing, error = self._listing(provider, key)
        if error:
            return _refused(error)
        return {"ok": True, "models": self._tagged(provider, listing)}

    @http.route("/cloud/assistant/usage", type="jsonrpc", auth="user")
    def assistant_usage(self, months=12):
        """Each user's tokens and approximate cost, per month."""
        self._check()
        try:
            months = min(24, max(1, int(months)))
        except (TypeError, ValueError):
            months = 12
        return {
            "rows": request.env["incubacloud.assistant.usage"]
            .sudo()
            ._usage_table(months)
        }
