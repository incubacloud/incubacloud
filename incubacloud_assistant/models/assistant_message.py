"""A message of an assistant conversation: the person's, the model's, a tool's.

Roles: ``user`` (the person), ``assistant`` (the model's text and the tools
it asked for), ``tool`` (a tool's answer, its secrets already masked),
``job`` (how a job the task waited for ended), ``notice`` (the panel
speaking to the person) and ``card`` (a confirmation that waits for the
person). The model sees the first four; the person sees all but the
tools' answers, of which the chat shows one line each.
"""

import json

from odoo import api, fields, models
from odoo.http import request

from odoo.addons.incubacloud_mcp.models.mcp_approval import APPROVAL_TTL
from odoo.addons.incubacloud_mcp.protocol.context import VIA_REQUEST_ATTR


class AssistantMessage(models.Model):
    """One message of a conversation."""

    _name = "incubacloud.assistant.message"
    _description = "Assistant message"
    _order = "id"

    conversation_id = fields.Many2one(
        "incubacloud.assistant.conversation",
        required=True,
        index=True,
        ondelete="cascade",
        readonly=True,
    )
    sequence = fields.Integer(
        readonly=True, index=True, help="Place in the model's context; 0 while pending."
    )
    pending = fields.Boolean(
        readonly=True, help="The person wrote it while a step ran: it waits."
    )
    role = fields.Selection(
        [
            ("user", "Person"),
            ("assistant", "Assistant"),
            ("tool", "Tool"),
            ("job", "Job result"),
            ("notice", "Notice"),
            ("card", "Confirmation"),
        ],
        required=True,
        readonly=True,
    )
    content = fields.Text(readonly=True)
    context_note = fields.Char(readonly=True, help="What the person had open.")
    tool_calls = fields.Text(readonly=True, help="JSON: the tools the model asked for.")
    tool_call_id = fields.Char(readonly=True, index=True)
    tool_name = fields.Char(readonly=True)
    is_error = fields.Boolean(readonly=True)
    raw_content = fields.Text(
        readonly=True, help="JSON: the provider's own answer, sent back as it came."
    )
    raw_provider = fields.Char(readonly=True)
    model_id = fields.Char(string="Model", readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    cache_read_tokens = fields.Integer(readonly=True)
    cache_write_tokens = fields.Integer(readonly=True)
    cost = fields.Float(digits=(16, 6), readonly=True)
    cost_known = fields.Boolean(readonly=True)
    in_context = fields.Boolean(
        default=True, readonly=True, help="False once summarized away."
    )
    ask_continue = fields.Boolean(readonly=True)
    approval_id = fields.Many2one(
        "incubacloud.mcp.approval", readonly=True, ondelete="set null"
    )
    card_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("confirmed", "Confirmed"),
            ("cancelled", "Cancelled"),
            ("expired", "Expired"),
        ],
        readonly=True,
    )
    production = fields.Boolean(readonly=True, help="The card acts on production.")

    def _calls(self):
        """Return the tool calls of this answer of the model, as dicts."""
        self.ensure_one()
        try:
            calls = json.loads(self.tool_calls or "[]")
        except ValueError:
            return []
        return [
            call
            for call in calls
            if isinstance(call, dict) and call.get("id") and call.get("name")
        ]

    @api.model
    def _label(self, name):
        """Return the line the chat shows for a call of tool *name*."""
        labels = {
            "search_docs": self.env._("Reading the documentation"),
            "search_code": self.env._("Searching the panel's code"),
            "read_code": self.env._("Reading the panel's code"),
            "repo_read": self.env._("Reading the repository"),
            "repo_create_branch": self.env._("Creating the branch"),
            "repo_write_files": self.env._("Writing the module"),
            "repo_open_pull_request": self.env._("Opening the pull request"),
            "repo_create": self.env._("Creating the repository"),
            "project_connect_repo": self.env._("Connecting the repository"),
            "create_instance": self.env._("Creating the staging"),
            "clone_to_staging": self.env._("Creating the staging"),
            "deploy_instance": self.env._("Deploying"),
            "rebuild_instance": self.env._("Rebuilding"),
            "read_logs": self.env._("Reading the logs"),
            "get_job": self.env._("Reading the job"),
            "panel_overview": self.env._("Looking at the panel"),
        }
        return labels.get(name) or self.env._("Running %s", name or "?")

    def _format(self):
        """Return what the chat shows of the message."""
        self.ensure_one()
        data = {
            "id": self.id,
            "role": self.role,
            "text": self.content or "",
            "pending": self.pending,
            "is_error": self.is_error,
            "ask_continue": self.ask_continue,
            "tool_name": self.tool_name or "",
            "create_date": fields.Datetime.to_string(self.create_date),
        }
        if self.role == "tool":
            data["label"] = self._label(self.tool_name)
            data["text"] = ""
        elif self.role == "job":
            data["label"] = self.env._("A job ended")
        elif self.role == "card":
            data["card"] = {"state": self.card_state, "production": self.production}
        elif self.role == "assistant":
            data["cost"] = round(self.cost, 4) if self.cost_known else None
        return data

    def _from_the_person(self):
        """Return whether this runs in the person's own request.

        A request an agent or the assistant makes carries the agent's mark
        (``VIA_REQUEST_ATTR``): it never confirms anything.
        """
        return not (request and getattr(request, VIA_REQUEST_ATTR, ""))

    def _confirm_card(self):
        """The person confirms the card: approve it once, and go on.

        :return: ``{ok}``, with ``error`` when it is no longer pending
        """
        self.ensure_one()
        conversation = self.conversation_id
        if (
            self.role != "card"
            or self.card_state != "pending"
            or not self._from_the_person()
        ):
            return {"ok": False, "error": self.env._("This confirmation is closed.")}
        conversation._lock()
        approval = self.approval_id.sudo()
        if approval.user_id.id == self.env.uid and approval._decide(True):
            # Ten minutes from the yes, not from the question: the step that
            # uses it may wait in the queue.
            approval.expires_at = fields.Datetime.now() + APPROVAL_TTL
            self.sudo().card_state = "confirmed"
            self._go_on()
            return {"ok": True}
        self.sudo().card_state = "expired"
        conversation._answer_call(
            self.tool_call_id,
            self.tool_name,
            "The confirmation expired before the person confirmed it. Ask again "
            "if it is still needed.",
            is_error=True,
        )
        self._go_on()
        return {
            "ok": False,
            "error": self.env._("This confirmation expired. The assistant was told."),
        }

    def _go_on(self):
        """Queue the next step once the person answered every card of the turn.

        A model's answer may ask for two confirmations: the task goes on
        after the last one, as one step, never two.
        """
        conversation = self.conversation_id
        if (
            conversation.state == "waiting_confirmation"
            and not conversation._pending_cards()
        ):
            conversation._enqueue_step()

    def _withdraw_card(self, text):
        """Cancel the card and its approval; answer its call with *text*."""
        self.ensure_one()
        self.approval_id.sudo()._withdraw()
        self.sudo().card_state = "cancelled"
        self.conversation_id._answer_call(
            self.tool_call_id, self.tool_name, text, is_error=True
        )

    def _cancel_card(self):
        """The person cancels the card: nothing runs, the model is told, it goes on."""
        self.ensure_one()
        if self.role != "card" or self.card_state != "pending":
            return {"ok": False, "error": self.env._("This confirmation is closed.")}
        self.conversation_id._lock()
        self._withdraw_card("The person cancelled this action.")
        self._go_on()
        return {"ok": True}
