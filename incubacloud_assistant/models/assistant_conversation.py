"""An assistant conversation: one person's, kept 30 days after its last activity.

Every person reads and deletes only their own conversations — a manager
too: the record rule is global, without a group. Nobody writes them
through the ORM: the access rights grant reading and deleting only, and
the server writes with sudo after checking whose they are. A task is a
chain of queue jobs of one step each (``incubacloud.assistant.engine``),
one task at a time: what the person writes meanwhile waits for the step in
course to end.
"""

import json
import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.incubacloud_mcp.models.mcp_tools import quoted

_logger = logging.getLogger(__name__)

#: Days a conversation is kept after its last activity.
RETENTION_DAYS = 30
#: A step running for this long without ending is taken for lost. A step is
#: one call to the provider (up to 600 s between reads, and three retries)
#: and its tools; the step's job checks it is still the conversation's
#: (``job_uuid``) before every write, so a step healed too early changes
#: nothing.
STUCK_AFTER = timedelta(hours=1)
#: The queue channel of the steps (``data/assistant_data.xml``).
CHANNEL = "root.assistant"
#: The bus notification type of the chat.
BUS = "cloud_assistant"
#: Characters of a conversation's title.
TITLE_CHARS = 60
#: Characters of one message of the person's.
MESSAGE_CHARS = 20_000
#: States in which a step is queued or runs, or a job of the task runs.
BUSY = ("working", "waiting_job")


class AssistantConversation(models.Model):
    """One person's conversation with the assistant."""

    _name = "incubacloud.assistant.conversation"
    _description = "Assistant conversation"
    _order = "last_activity desc, id desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade", readonly=True
    )
    name = fields.Char(readonly=True)
    lang = fields.Char(readonly=True)
    provider = fields.Char(readonly=True)
    model_id = fields.Char(string="Model", readonly=True)
    state = fields.Selection(
        [
            ("idle", "Idle"),
            ("working", "Working"),
            ("waiting_job", "Waiting for a job"),
            ("waiting_confirmation", "Waiting for a confirmation"),
            ("stopped", "Stopped"),
            ("error", "Error"),
        ],
        default="idle",
        required=True,
        readonly=True,
        index=True,
    )
    stop_requested = fields.Boolean(readonly=True)
    tainted = fields.Boolean(
        readonly=True,
        help="Since the person's last message the task read content others can "
        "write (logs, files, mail): every change asks for a card.",
    )
    step_count = fields.Integer(readonly=True, help="Calls to the model in this task.")
    step_started_at = fields.Datetime(readonly=True)
    job_uuid = fields.Char(readonly=True, help="The queue job of the step in course.")
    running_call_id = fields.Char(
        readonly=True,
        help="The tool call that runs now. Left behind by a step that died, its "
        "outcome is unknown: the next step says so instead of running it again.",
    )
    waiting_job_id = fields.Many2one(
        "cloud.job", readonly=True, index=True, ondelete="set null"
    )
    cost = fields.Float(digits=(16, 6), readonly=True, help="Euros, approximately.")
    cost_unknown = fields.Boolean(
        readonly=True, help="Some call used a model whose price is not known."
    )
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    last_activity = fields.Datetime(
        readonly=True, index=True, default=fields.Datetime.now
    )
    unread = fields.Boolean(readonly=True)
    summary = fields.Text(
        readonly=True,
        help="What the earlier part of the conversation said, summarized.",
    )
    message_ids = fields.One2many(
        "incubacloud.assistant.message", "conversation_id", readonly=True
    )

    # ── Creation, messages, bus ───────────────────────────────────────────

    @api.model
    def _start(self, model_id=""):
        """Open a conversation of the current user, on an allowed model."""
        config = self.env["cloud.settings"]._assistant_config()
        model = model_id if model_id in config.allowed_models else config.default_model
        return (
            self.sudo()
            .create(
                {
                    "user_id": self.env.uid,
                    "lang": self.env.user.lang or "en_US",
                    "provider": config.provider,
                    "model_id": model,
                }
            )
            .sudo(False)
        )

    def _lock(self):
        """Hold the conversation's row until the transaction ends.

        Two requests of the person (two messages, a message and a
        confirmation) change it one after the other, never both on the
        same old state.
        """
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM incubacloud_assistant_conversation "
            "WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()

    def _next_sequence(self):
        """Return the sequence the next message in the context takes."""
        self.ensure_one()
        self.env["incubacloud.assistant.message"].flush_model(["sequence"])
        self.env.cr.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM incubacloud_assistant_message "
            "WHERE conversation_id = %s",
            (self.id,),
        )
        return self.env.cr.fetchone()[0]

    def _add_message(self, vals, pending=False):
        """Add a message and tell the person's open panels.

        :param dict vals: the message's fields
        :param bool pending: the person's message waits for the step in
            course: it has no place in the context until delivered
        :return: the message, as the conversation's environment sees it
        """
        self.ensure_one()
        message = (
            self.env["incubacloud.assistant.message"]
            .sudo()
            .create(
                {
                    **vals,
                    "conversation_id": self.id,
                    "pending": pending,
                    "sequence": 0 if pending else self._next_sequence(),
                }
            )
        )
        self.sudo().last_activity = fields.Datetime.now()
        self._notify({"kind": "message", "message": message._format()})
        return message.with_env(self.env)

    def _answer_call(self, call_id, tool_name, text, is_error=False):
        """Answer a tool call of the model with *text* (a result or why not)."""
        return self._add_message(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "content": text,
                "is_error": is_error,
            }
        )

    def _notify(self, payload):
        """Send *payload* to the owner's panels on the assistant's bus channel."""
        self.ensure_one()
        owner = self.env["res.users"].sudo().browse(self.sudo().user_id.id)
        owner._bus_send(BUS, {"conversation_id": self.id, **payload})

    def _notify_state(self):
        """Tell the owner's panels the conversation's state."""
        self._notify({"kind": "state", "state": self.state, "unread": self.unread})

    def _format(self):
        """Return what the panel lists of the conversation."""
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name or "",
            "state": self.state,
            "model_id": self.model_id or "",
            "cost": round(self.cost, 4),
            "cost_unknown": self.cost_unknown,
            "last_activity": fields.Datetime.to_string(self.last_activity),
            "unread": self.unread,
        }

    # ── What the model's calls wait for ────────────────────────────────────

    def _open_calls(self):
        """Return the tool calls of the last answer of the model still unanswered.

        :rtype: list[dict]
        """
        self.ensure_one()
        Message = self.env["incubacloud.assistant.message"].sudo()
        last = Message.search(
            [
                ("conversation_id", "=", self.id),
                ("role", "=", "assistant"),
                ("in_context", "=", True),
            ],
            order="sequence desc, id desc",
            limit=1,
        )
        if not last:
            return []
        answered = set(
            Message.search(
                [
                    ("conversation_id", "=", self.id),
                    ("role", "=", "tool"),
                    ("sequence", ">", last.sequence),
                ]
            ).mapped("tool_call_id")
        )
        return [call for call in last._calls() if call["id"] not in answered]

    def _close_open_calls(self, text):
        """Answer every call still open with *text*: the task will not run them."""
        for call in self._open_calls():
            self._answer_call(call["id"], call["name"], text, is_error=True)

    def _pending_cards(self):
        """Return the cards that wait for the person."""
        self.ensure_one()
        return (
            self.env["incubacloud.assistant.message"]
            .sudo()
            .search(
                [
                    ("conversation_id", "=", self.id),
                    ("role", "=", "card"),
                    ("card_state", "=", "pending"),
                ]
            )
            .with_env(self.env)
        )

    # ── What the person does ───────────────────────────────────────────────

    @api.model
    def _screen_note(self, context):
        """Say in one line what the person has open, from the panel's own records.

        Only what the person may read is named; the panel's client sends
        ids, never names.

        :param dict context: ``{route, project_id, instance_id, host_id}``
        :rtype: str
        """
        context = context if isinstance(context, dict) else {}
        parts = []
        for key, model, label in (
            ("instance_id", "cloud.instance", "instance"),
            ("project_id", "cloud.project", "project"),
            ("host_id", "cloud.host", "server"),
        ):
            value = context.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            record = self.env[model].browse(value)
            try:
                if not record.exists():
                    continue
                record.check_access("read")
                name = record.name
            except AccessError:
                continue
            extra = f", {record.environment}" if model == "cloud.instance" else ""
            parts.append(f"{label} {quoted(name)} (id {value}{extra})")
        route = str(context.get("route") or "")[:40]
        if not parts and not route:
            return ""
        return "Screen: " + (", ".join(parts) if parts else route)

    def _post(self, text, context=None):
        """The person writes *text*: it starts a task, or joins the one in course.

        A card waiting for the person is cancelled: writing instead of
        confirming is a no, and the model is told.

        :return: the person's message
        :raises UserError: for an empty message
        """
        self.ensure_one()
        self._lock()
        text = (text or "").strip()
        if not text:
            raise UserError(self.env._("Write a message first."))
        if self.state == "waiting_confirmation":
            for card in self._pending_cards():
                card._withdraw_card(
                    "The person wrote a new message instead of confirming."
                )
            self._close_open_calls("Not run: the person wrote a new message.")
        busy = self.state in BUSY
        message = self._add_message(
            {
                "role": "user",
                "content": text[:MESSAGE_CHARS],
                "context_note": self._screen_note(context),
            },
            pending=busy,
        )
        if not self.name:
            self.sudo().name = " ".join(text.split())[:TITLE_CHARS]
        if not busy:
            # The person spoke and nothing runs: what the task read before no
            # longer steers it. While a step runs, the mark stays until the
            # message enters the context (the engine's ``_deliver_pending``):
            # the calls that step decided keep asking.
            self.sudo().tainted = False
            self.sudo().write({"step_count": 0, "stop_requested": False})
            self._enqueue_step()
        return message

    def _enqueue_step(self):
        """Queue the task's next step, as the owner, in the conversation's language."""
        self.ensure_one()
        owner = self.sudo().user_id
        job = (
            self.with_user(owner)
            .with_context(lang=self.lang or owner.lang)
            .with_delay(
                channel=CHANNEL,
                max_retries=1,
                description=f"Assistant step, conversation {self.id}",
            )
            ._assistant_step()
        )
        now = fields.Datetime.now()
        self.sudo().write(
            {
                "state": "working",
                "job_uuid": job.uuid,
                "step_started_at": now,
                "last_activity": now,
            }
        )
        self._notify_state()

    def _assistant_step(self):
        """Run one step of the task: the queue job (see the engine)."""
        self.env["incubacloud.assistant.engine"]._step(self)

    def _halt(self, state, text, error=False, ask_continue=False, unread=True):
        """End the task's turn in *state*, telling the person *text*.

        :param bool ask_continue: the notice offers to go on (step cap)
        :param bool unread: the button shows that something happened while
            the person was away (not while the task only waits for a job)
        """
        self.ensure_one()
        if text:
            self._add_message(
                {
                    "role": "notice",
                    "content": text,
                    "is_error": error,
                    "ask_continue": ask_continue,
                }
            )
        self.sudo().write(
            {
                "state": state,
                "unread": unread,
                "stop_requested": False,
                "step_started_at": False,
                "job_uuid": False,
            }
        )
        self._notify_state()

    def _stop(self):
        """The person stops the task: at once when nothing runs, else at the next check.

        What was done stays done (a commit on the branch, a staging).
        """
        self.ensure_one()
        self._lock()
        if self.state == "working":
            self.sudo().stop_requested = True
            return
        if self.state not in ("waiting_job", "waiting_confirmation"):
            return
        for card in self._pending_cards():
            card._withdraw_card("The person stopped the task.")
        self._close_open_calls("Not run: the person stopped the task.")
        self.sudo().waiting_job_id = False
        self._halt("stopped", self.env._("Stopped. What was done stays as it is."))

    def _retry(self):
        """Go on after an error or a stop; return whether a step was queued."""
        self.ensure_one()
        self._lock()
        if self.state not in ("error", "stopped"):
            return False
        self.sudo().stop_requested = False
        self._enqueue_step()
        return True

    def _heal(self):
        """Take a step that will never end for lost: error, and Retry; and go on
        with a task whose job ended without telling it.

        Lost: running for longer than :data:`STUCK_AFTER`, or its queue job
        ended (or is gone) and nothing came after it. A conversation that
        waits for a job that ended, or is gone, missed the job's own hook
        (``cloud.job._after_job_terminal``): it goes on as the hook would
        have made it.
        """
        self.ensure_one()
        if self.state == "waiting_job":
            job = self.sudo().waiting_job_id
            if not job or job.state in job._terminal_states:
                self.env["incubacloud.assistant.engine"]._job_ended(
                    self, job, job.state if job else "gone"
                )
            return
        if self.state != "working":
            return
        lost = bool(
            self.step_started_at
            and self.step_started_at < fields.Datetime.now() - STUCK_AFTER
        )
        if not lost and self.job_uuid:
            job = (
                self.env["queue.job"]
                .sudo()
                .search([("uuid", "=", self.job_uuid)], limit=1)
            )
            lost = not job or job.state in ("done", "failed", "cancelled")
        if lost:
            self._halt(
                "error",
                self.env._("The assistant stopped unexpectedly. Retry to go on."),
                error=True,
            )

    @api.model
    def _cron_purge(self):
        """Delete the conversations without activity for 30 days, whatever their state.

        Every step and every message touches ``last_activity``: one still
        working after a month is lost, and its job, if it ever ran again,
        would find no conversation.
        """
        for conversation in self.sudo().search([("state", "in", BUSY)]):
            # A step lost, or a job that ended unseen: error, or going on. One
            # that cannot be healed never keeps the others from their purge.
            try:
                with self.env.cr.savepoint():
                    conversation._heal()
            except Exception:  # logged; the purge goes on
                _logger.exception(
                    "assistant: conversation %s could not be healed", conversation.id
                )
        limit = fields.Datetime.now() - timedelta(days=RETENTION_DAYS)
        self.sudo().search([("last_activity", "<", limit)]).unlink()

    def _tool_calls_json(self, calls):
        """Return *calls* (``ToolCall``) as the message stores them."""
        return json.dumps(
            [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
            ensure_ascii=False,
        )
