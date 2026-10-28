"""The assistant's chat: what the panel's side panel calls.

Every route is the signed-in person's: they read and change their own
conversations only (record rules, and checked again here). Writing,
confirming, cancelling, retrying, changing the model and accepting the
notice need the assistant on for them; listing, reading, deleting and
stopping what they have do not: their conversations stay theirs until they
expire (30 days). Cards are answered here and only here: the model has no
tool that reaches these routes, and the MCP excludes all of them.
"""

import urllib.parse
from datetime import timedelta

from odoo import _, fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from odoo.addons.incubacloud.controllers._rate_limit import Rule, rate_gate_json

from ..pricing import model_usable, price_for
from ..providers import RECOMMENDED, provider_label

#: Days of conversations the side panel lists.
LISTED_DAYS = 30
#: Messages one person may send per minute.
SEND_PER_MINUTE = 20
#: Hosts whose links the chat always makes clickable.
LINK_HOSTS = ("docs.incubacloud.io", "github.com")


class AssistantChat(http.Controller):
    """Routes of the assistant's side panel."""

    @staticmethod
    def _available():
        """Raise unless the assistant is on for the signed-in person."""
        Settings = request.env["cloud.settings"]
        if not Settings._assistant_available_for(request.env.user):
            raise AccessError(_("The assistant is not available to you."))

    @staticmethod
    def _conversation(conversation_id):
        """Return the person's conversation, or raise."""
        try:
            conversation = request.env["incubacloud.assistant.conversation"].browse(
                int(conversation_id)
            )
        except (TypeError, ValueError) as exc:
            raise UserError(_("This conversation does not exist.")) from exc
        if not conversation.exists():
            raise UserError(_("This conversation does not exist."))
        conversation.check_access("read")
        if conversation.user_id != request.env.user:
            raise AccessError(_("This conversation is not yours."))
        return conversation

    @staticmethod
    def _card(message_id):
        """Return a card of the person's own conversations, or raise."""
        try:
            message = request.env["incubacloud.assistant.message"].browse(
                int(message_id)
            )
        except (TypeError, ValueError) as exc:
            raise UserError(_("This confirmation does not exist.")) from exc
        if not message.exists() or message.role != "card":
            raise UserError(_("This confirmation does not exist."))
        message.check_access("read")
        if message.conversation_id.user_id != request.env.user:
            raise AccessError(_("This confirmation is not yours."))
        return message

    @staticmethod
    def _models(config):
        """Return the allowed models as the selector shows them.

        ``usable`` is false for a model without a known price while the
        panel has a monthly limit: the selector shows it disabled.
        """
        recommended = RECOMMENDED.get(config.provider, ())
        return [
            {
                "id": model,
                "name": (config.listing(model) or {}).get("name") or model,
                "recommended": model in recommended,
                "price_known": price_for(config.provider, model, config.listing)
                is not None,
                "usable": model_usable(config, model),
            }
            for model in config.allowed_models
        ]

    @staticmethod
    def _model_refusal(config, model_id):
        """Return why *model_id* cannot be picked, or None when it can."""
        if model_id not in config.allowed_models:
            return _("This model is not allowed on this panel.")
        if not model_usable(config, model_id):
            return _(
                "This model has no known price, and this panel has a monthly limit: "
                "it cannot be used. A manager can remove the limit or allow a model "
                "with a known price."
            )
        return None

    @staticmethod
    def _link_hosts():
        """Return the hosts whose links the chat makes clickable.

        The documentation, GitHub, the panel itself and the domains of the
        instances the person sees, each by its own name: not a server's
        wildcard, which on a shared server is also other customers'. Any
        other address in an answer stays text: a link written from a log
        would be one click from sending something away.
        """
        hosts = set(LINK_HOSTS)
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        panel = urllib.parse.urlsplit(base).hostname
        if panel:
            hosts.add(panel)
        instances = request.env["cloud.instance"].search([])
        hosts |= {
            hostname
            for hostname in instances.sudo().domain_ids.mapped("hostname")
            if hostname
        }
        return sorted(hosts)

    @http.route("/cloud/assistant/status", type="jsonrpc", auth="user")
    def assistant_status(self):
        """Whether the person may use the assistant, and what the button and panel show.

        Off for them, how many conversations they still have: the button
        shows them to read and delete until they expire.
        """
        Settings = request.env["cloud.settings"]
        if not Settings._assistant_available_for(request.env.user):
            kept = request.env["incubacloud.assistant.conversation"].search_count([])
            return {"available": False, "conversations": kept}
        config = Settings._assistant_config()
        cost, unpriced = (
            request.env["incubacloud.assistant.usage"]
            .sudo()
            ._month_cost(request.env.uid)
        )
        unread = request.env["incubacloud.assistant.conversation"].search_count(
            [("unread", "=", True)]
        )
        return {
            "available": True,
            "notice_needed": not request.env.user.sudo().assistant_notice_at,
            "provider": config.provider,
            "provider_label": provider_label(config.provider),
            "usage": {
                "cost": round(cost, 2),
                "limit": config.monthly_limit,
                "unpriced": unpriced,
            },
            "unread": unread,
            "models": self._models(config),
            "default_model": config.default_model,
            "link_hosts": self._link_hosts(),
        }

    @http.route("/cloud/assistant/accept_notice", type="jsonrpc", auth="user")
    def assistant_accept_notice(self):
        """The person read what the assistant sends to the provider."""
        self._available()
        request.env.user.sudo().assistant_notice_at = fields.Datetime.now()
        return {"ok": True}

    @http.route("/cloud/assistant/conversations", type="jsonrpc", auth="user")
    def assistant_conversations(self):
        """The person's conversations of the last 30 days, the latest first."""
        since = fields.Datetime.now() - timedelta(days=LISTED_DAYS)
        conversations = request.env["incubacloud.assistant.conversation"].search(
            [("last_activity", ">=", since)], limit=100
        )
        return {"conversations": [c._format() for c in conversations]}

    @http.route("/cloud/assistant/conversation", type="jsonrpc", auth="user")
    def assistant_conversation(self, conversation_id):
        """One conversation and its messages; it is read now."""
        conversation = self._conversation(conversation_id)
        conversation._heal()
        if conversation.unread:
            conversation.sudo().unread = False
        messages = conversation.message_ids.sorted(lambda m: (m.pending, m.id))
        return {
            "conversation": conversation._format(),
            "messages": [message._format() for message in messages],
        }

    @http.route("/cloud/assistant/send", type="jsonrpc", auth="user")
    def assistant_send(self, text, conversation_id=None, context=None, model_id=None):
        """The person writes: in a new conversation, or in one of theirs."""
        self._available()
        limited = rate_gate_json(
            Rule(
                f"assistant_send_user:{request.env.uid}",
                _("You are writing too fast. Try again in a minute."),
                max_per_window=SEND_PER_MINUTE,
                log_tag=f"assistant send user={request.env.uid}",
            )
        )
        if limited:
            return limited
        if not request.env.user.sudo().assistant_notice_at:
            return {
                "ok": False,
                "error": _("Read first what the assistant sends to the AI provider."),
            }
        if model_id:
            refusal = self._model_refusal(
                request.env["cloud.settings"]._assistant_config(), model_id
            )
            if refusal:
                return {"ok": False, "error": refusal}
        Conversation = request.env["incubacloud.assistant.conversation"]
        conversation = (
            self._conversation(conversation_id)
            if conversation_id
            else Conversation._start(model_id or "")
        )
        try:
            message = conversation._post(
                text, context if isinstance(context, dict) else {}
            )
        except UserError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "conversation": conversation._format(),
            "message": message._format(),
        }

    @http.route("/cloud/assistant/set_model", type="jsonrpc", auth="user")
    def assistant_set_model(self, conversation_id, model_id):
        """Change the conversation's model, among the allowed ones."""
        self._available()
        config = request.env["cloud.settings"]._assistant_config()
        refusal = self._model_refusal(config, model_id)
        if refusal:
            return {"ok": False, "error": refusal}
        conversation = self._conversation(conversation_id)
        conversation.sudo().write({"model_id": model_id, "provider": config.provider})
        return {"ok": True}

    @http.route("/cloud/assistant/stop", type="jsonrpc", auth="user")
    def assistant_stop(self, conversation_id):
        """Stop the conversation's task (at the next step's check if one runs)."""
        conversation = self._conversation(conversation_id)
        conversation._stop()
        return {"ok": True, "conversation": conversation._format()}

    @http.route("/cloud/assistant/retry", type="jsonrpc", auth="user")
    def assistant_retry(self, conversation_id):
        """Go on after an error or a stop."""
        self._available()
        conversation = self._conversation(conversation_id)
        return {"ok": conversation._retry(), "conversation": conversation._format()}

    @http.route("/cloud/assistant/confirm", type="jsonrpc", auth="user")
    def assistant_confirm(self, message_id):
        """The person confirms a card."""
        self._available()
        return self._card(message_id)._confirm_card()

    @http.route("/cloud/assistant/cancel", type="jsonrpc", auth="user")
    def assistant_cancel(self, message_id):
        """The person cancels a card."""
        self._available()
        return self._card(message_id)._cancel_card()

    @http.route("/cloud/assistant/delete", type="jsonrpc", auth="user")
    def assistant_delete(self, conversation_id):
        """Delete one of the person's conversations; its task stops first."""
        conversation = self._conversation(conversation_id)
        conversation._stop()
        conversation.unlink()
        return {"ok": True}
