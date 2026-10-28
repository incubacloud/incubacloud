"""One step of an assistant task, as a queue job of the conversation's owner.

A step reads the settings again (the key may have changed, the assistant
may be off for this user now), asks the provider once and runs the tools
the model asked for, then queues the next step, or stops: the model is
done, a card waits for the person, a job runs, the step cap or the monthly
limit is reached, the person stopped it, or the provider failed.

The step's own transaction writes nothing, as a ``cloud.job`` executor's
does not (``AbstractExecutor._durable_env``): queue_job forbids committing
it midway and a step lasts minutes. What the step records — the model's
answer, each tool's answer with the tool's own effects, the state — is
written on a cursor of its own and committed at once: the person sees it
as it happens, and a retry of the job never runs a tool twice.
"""

import json
import logging
import re
import time
from contextlib import contextmanager

from odoo import api, models
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY

from odoo.addons.incubacloud_mcp.protocol.errors import McpError

from .. import pricing
from ..connectors import connector_for
from ..connectors.base import ChatRequest, ProviderError, ToolCall, Turn, same_model
from ..request_scope import call_context, tool_request
from .assistant_prompt import system_prompt
from .assistant_tools import THIRD_PARTY_READS

_logger = logging.getLogger(__name__)

#: Seconds between attempts when the provider is busy or down.
RETRY_DELAYS = (2, 8, 30)
#: Tries of a write of the step's that meets a concurrent change.
WRITE_TRIES = 5
#: Characters of a job's log the model reads when it ends.
JOB_LOG_CHARS = 8000
#: Pauses go through here, so that tests patch them out.
_sleep = time.sleep
#: A closing data tag, in any case.
_CLOSING_RE = re.compile(r"</(data)", re.IGNORECASE)


def as_data(source, text):
    """Wrap *text* as data from *source*: never instructions.

    A closing tag inside it, in any case, is broken, so the text cannot end
    the wrapper early and speak as the panel.
    """
    safe = _CLOSING_RE.sub(r"<\\/\1", text or "")
    return f'<data source="{source}">\n{safe}\n</data>'


class AssistantEngine(models.AbstractModel):
    """The steps of the assistant's tasks."""

    _name = "incubacloud.assistant.engine"
    _description = "Steps of the assistant's tasks"

    # ── Cursors ───────────────────────────────────────────────────────────

    @contextmanager
    def _durable(self):
        """Yield this user's environment on its own cursor, committed at the end."""
        with self.env.registry.cursor() as cr:
            yield api.Environment(cr, self.env.uid, dict(self.env.context))
            cr.commit()

    @api.model
    def _conversation_in(self, env, conversation_id):
        """Return the conversation in *env* while this step is still its own.

        Empty when it is gone, or when another step took the task over: the
        queue job this runs in (``job_uuid`` in the context, as queue_job
        sets it) is no longer the conversation's, as after a step healed as
        lost and retried. A late step then writes nothing.
        """
        conv = (
            env["incubacloud.assistant.conversation"].browse(conversation_id).exists()
        )
        job = env.context.get("job_uuid")
        if conv and job and conv.job_uuid != job:
            return conv.browse()
        return conv

    # ── The step ─────────────────────────────────────────────────────────

    @api.model
    def _step(self, conversation):
        """Run one step of *conversation*'s task (the queue job).

        Never raises: a failure leaves the conversation in error, with
        Retry, and what was done stays done.
        """
        conversation_id = conversation.id
        try:
            self._run_step(conversation_id)
        except Exception:  # the person gets Retry; the log keeps why
            _logger.exception(
                "assistant: a step of conversation %s failed", conversation_id
            )
            with self._durable() as env:
                conv = self._conversation_in(env, conversation_id)
                if conv:
                    conv._halt(
                        "error",
                        env._(
                            "The assistant stopped because of an error. Retry to go "
                            "on; what was done stays."
                        ),
                        error=True,
                    )

    @api.model
    def _run_step(self, conversation_id):
        """The checks, the provider's answer, its tools, and what comes next."""
        checked = self._check(conversation_id)
        if checked is None:
            return
        config, key, open_calls = checked
        if open_calls:
            self._run_calls(conversation_id, open_calls)
            return
        asked = self._ask(conversation_id, config, key)
        if asked is None:
            return
        response, model = asked
        if not same_model(model, response.model):
            self._wrong_model(conversation_id, config, model, response)
            return
        calls = self._record_answer(conversation_id, config, response, model)
        if response.stop == "max_tokens" and not calls and not response.text:
            self._finish(
                conversation_id,
                "error",
                self.env._(
                    "The answer was cut at the output limit before it said anything. "
                    "Retry, or ask for less at once."
                ),
                error=True,
            )
            return
        if response.stop == "refusal":
            self._finish(
                conversation_id,
                "idle",
                self.env._(
                    "The AI provider declined this request. Put it another way, or ask "
                    "something else."
                ),
            )
            return
        if response.stop == "max_tokens" and calls:
            self._refuse_calls(
                conversation_id,
                calls,
                "Not run: your answer was cut at the output limit. Do the work in "
                "smaller steps, with fewer files per commit.",
            )
            self._next(conversation_id)
            return
        if not calls:
            self._finish(conversation_id, "idle", "")
            return
        self._run_calls(conversation_id, calls)

    @api.model
    def _check(self, conversation_id):
        """Return ``(config, key, calls still open)``, or None when the step stops.

        The settings and the key are read together, in one transaction: a
        key is never sent to a provider it was not saved for.
        """
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if not conv or conv.state != "working":
                return None
            if conv.stop_requested:
                conv._close_open_calls("Not run: the person stopped the task.")
                conv._halt("stopped", env._("Stopped. What was done stays as it is."))
                return None
            Settings = env["cloud.settings"]
            if not Settings._assistant_available_for(env.user):
                conv._close_open_calls("Not run: the assistant was switched off.")
                conv._halt(
                    "stopped",
                    env._(
                        "The assistant is off, or you may no longer use it. This "
                        "conversation is kept."
                    ),
                )
                return None
            config = Settings._assistant_config()
            _provider, key = Settings._assistant_credentials()
            open_calls = [
                ToolCall(call["id"], call["name"], call.get("arguments") or {})
                for call in conv._open_calls()
            ]
            if open_calls:
                return config, key, open_calls
            spent, _unpriced = env["incubacloud.assistant.usage"]._month_cost(env.uid)
            if config.monthly_limit and spent >= config.monthly_limit:
                conv._halt(
                    "stopped",
                    env._(
                        "You reached your monthly limit of %(limit)s €. The assistant "
                        "goes on next month, or when a manager raises the limit.",
                        limit=f"{config.monthly_limit:.2f}",
                    ),
                )
                return None
            if conv.step_count >= config.step_cap:
                conv._halt(
                    "idle",
                    env._(
                        "I have taken %s steps on this task. Shall I go on?",
                        config.step_cap,
                    ),
                    ask_continue=True,
                )
                return None
            return config, key, []

    # ── The provider ─────────────────────────────────────────────────────

    @api.model
    def _ask(self, conversation_id, config, key):
        """Return ``(the model's next answer, the model asked)``, or None when stopped.

        A model without a known price is refused while the panel has a
        monthly limit (``pricing.model_usable``): the limit could not count it.
        """
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            model = self._model_for(conv, config)
            if not pricing.model_usable(config, model):
                conv._halt(
                    "error",
                    env._(
                        "The model %s has no known price, and this panel has a "
                        "monthly limit: the assistant cannot count what it costs. A "
                        "manager can remove the limit or allow a model with a known "
                        "price in Settings → Assistant.",
                        model,
                    ),
                    error=True,
                )
                return None
            self._deliver_pending(conv)
            request = self._request(conv, model)
            conv._notify({"kind": "progress", "label": env._("Thinking…")})
        try:
            connector = connector_for(config.provider, key)
        except ProviderError as error:
            self._provider_failed(conversation_id, error)
            return None
        attempt, summarized = 0, False
        while True:
            try:
                return connector.chat(request), model
            except ProviderError as error:
                if error.usage:
                    # A failure the provider still billed: it counts.
                    self._record_cost(
                        conversation_id, config, error.billed(model), model
                    )
                if error.retryable and attempt < len(RETRY_DELAYS):
                    pause = error.retry_after
                    _sleep(pause if pause is not None else RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                if error.kind == "context_too_long" and not summarized:
                    summarized = self._summarize(
                        conversation_id, connector, model, config
                    )
                    if summarized:
                        with self._durable() as env:
                            request = self._request(
                                self._conversation_in(env, conversation_id), model
                            )
                        continue
                self._provider_failed(conversation_id, error)
                return None

    @api.model
    def _model_for(self, conv, config):
        """Return the conversation's model, or the default one if no longer allowed."""
        if conv.provider == config.provider and conv.model_id in config.allowed_models:
            return conv.model_id
        model = config.default_model
        conv.sudo().write({"model_id": model, "provider": config.provider})
        conv._add_message(
            {
                "role": "notice",
                "content": conv.env._(
                    "This conversation now uses %s: the model it used is no longer "
                    "allowed.",
                    model,
                ),
            }
        )
        return model

    @api.model
    def _deliver_pending(self, conv):
        """Give what the person wrote during the last step its place in the context."""
        pending = (
            conv.env["incubacloud.assistant.message"]
            .sudo()
            .search(
                [("conversation_id", "=", conv.id), ("pending", "=", True)], order="id"
            )
        )
        for message in pending:
            message.write({"pending": False, "sequence": conv._next_sequence()})
            # The chat drops its "queued" mark.
            conv._notify({"kind": "message", "message": message._format()})
        if pending:
            # The person's word enters the context now: from here on, what the
            # task read before no longer steers it alone.
            conv.sudo().tainted = False

    @api.model
    def _request(self, conv, model):
        """Return the provider's request for the conversation as it stands."""
        language = (
            conv.env["res.lang"]
            .with_context(active_test=False)
            .search([("code", "=", conv.lang)], limit=1)
            .name
            or "English"
        )
        return ChatRequest(
            model=model,
            system=system_prompt(language),
            tools=conv.env["incubacloud.assistant.tools"]._definitions(),
            turns=self._turns(conv),
        )

    @api.model
    def _turns(self, conv):
        """Return the conversation in the model's context, as ``Turn``.

        The person's text with what they had open, masked of the panel's
        secrets; the model's answers with their own content; the tools'
        answers and the jobs' ends as data.
        """
        messages = (
            conv.env["incubacloud.assistant.message"]
            .sudo()
            .search(
                [
                    ("conversation_id", "=", conv.id),
                    ("in_context", "=", True),
                    ("pending", "=", False),
                    ("role", "in", ("user", "assistant", "tool", "job")),
                ],
                order="sequence, id",
            )
        )
        scrubber = conv.env["incubacloud.mcp.secrets"]._scrubber()
        turns = []
        if conv.summary:
            turns.append(
                Turn(
                    "user", as_data("summary of the earlier conversation", conv.summary)
                )
            )
        for message in messages:
            if message.role == "user":
                text = scrubber.text(message.content or "")
                if message.context_note:
                    # Records' names are written by others: data, as a tool's answer.
                    note = as_data("screen", scrubber.text(message.context_note))
                    text = f"{note}\n{text}"
                turns.append(Turn("user", text))
            elif message.role == "assistant":
                raw = json.loads(message.raw_content) if message.raw_content else None
                turns.append(
                    Turn(
                        "assistant",
                        message.content or "",
                        tuple(
                            ToolCall(
                                call["id"], call["name"], call.get("arguments") or {}
                            )
                            for call in message._calls()
                        ),
                        raw=raw,
                        raw_provider=message.raw_provider or "",
                    )
                )
            elif message.role == "tool":
                turns.append(
                    Turn(
                        "tool",
                        as_data(f"tool {message.tool_name}", message.content or ""),
                        tool_call_id=message.tool_call_id,
                        tool_name=message.tool_name,
                        is_error=message.is_error,
                    )
                )
            else:
                turns.append(Turn("user", as_data("job", message.content or "")))
        return tuple(turns)

    @api.model
    def _record_answer(self, conversation_id, config, response, model):
        """Record the model's answer, its cost and the month's usage; return its calls.

        Priced at *model*, the one asked for, when the answer names a dated
        version of it.
        """
        cost = pricing.response_cost(config.provider, response, config.listing, model)
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if not conv:
                return ()
            scrubber = env["incubacloud.mcp.secrets"]._scrubber()
            usage = response.usage
            conv._add_message(
                {
                    "role": "assistant",
                    "content": scrubber.text(response.text),
                    "tool_calls": conv._tool_calls_json(response.tool_calls),
                    "raw_content": json.dumps(response.raw)
                    if response.raw is not None
                    else False,
                    "raw_provider": config.provider,
                    "model_id": response.model,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "cost": cost or 0.0,
                    "cost_known": cost is not None,
                }
            )
            conv.sudo().write(
                {
                    "step_count": conv.step_count + 1,
                    "cost": conv.cost + (cost or 0.0),
                    "cost_unknown": conv.cost_unknown or cost is None,
                    "input_tokens": conv.input_tokens
                    + usage.input_tokens
                    + usage.cache_read_tokens
                    + usage.cache_write_tokens,
                    "output_tokens": conv.output_tokens + usage.output_tokens,
                }
            )
            env["incubacloud.assistant.usage"]._add(
                env.uid, config.provider, response.model, usage, cost
            )
        return response.tool_calls

    @api.model
    def _record_cost(self, conversation_id, config, response, requested=""):
        """Count a call that leaves no answer in the conversation (a summary, a
        refused answer): its cost in the conversation and the month's usage.

        :param str requested: the model asked for, whose price a dated
            version of it answers at
        """
        cost = pricing.response_cost(
            config.provider, response, config.listing, requested
        )
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if not conv:
                return
            conv.sudo().write(
                {
                    "cost": conv.cost + (cost or 0.0),
                    "cost_unknown": conv.cost_unknown or cost is None,
                }
            )
            env["incubacloud.assistant.usage"]._add(
                env.uid, config.provider, response.model or "?", response.usage, cost
            )

    @api.model
    def _wrong_model(self, conversation_id, config, model, response):
        """Refuse an answer of another model than the one asked for, and record it.

        The answer is dropped (it never enters the conversation); what it
        cost is counted; the log, the audit and the person are told.
        """
        _logger.warning(
            "assistant: %s answered conversation %s with model %r instead of %r",
            config.provider,
            conversation_id,
            response.model,
            model,
        )
        # Priced at the model that answered: *model* only names what was asked.
        self._record_cost(conversation_id, config, response, model)
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if not conv:
                return
            env["cloud.audit.log"].sudo().create(
                {
                    "user_id": conv.user_id.id,
                    "action": "Assistant: answer of another model refused",
                    "via_client": f"Assistant (conversation {conversation_id})",
                    "details": f"asked {model!r}, {config.provider} answered "
                    f"{response.model!r}"[:2000],
                }
            )
            conv._halt(
                "error",
                env._(
                    "The AI provider answered with another model (%(answered)s) than "
                    "the one allowed (%(model)s). Its answer was set aside. Retry, or "
                    "tell a manager of the panel.",
                    answered=response.model,
                    model=model,
                ),
                error=True,
            )

    @api.model
    def _provider_failed(self, conversation_id, error):
        """Put the conversation in error, saying what the provider's failure means."""
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if not conv:
                return
            manager = env["cloud.security.mixin"]._has_cloud_group(
                "group_cloud_manager"
            )
            where = (
                env._("Change it in Settings → Assistant.")
                if manager
                else env._("Tell a manager of the panel.")
            )
            texts = {
                "invalid_key": env._("The AI provider refused the panel's key.")
                + " "
                + where,
                "quota": env._(
                    "The AI provider's account has no credit left. A manager can check "
                    "it on the provider's site."
                ),
                "rate_limited": env._("The AI provider is busy. Retry in a moment."),
                "unavailable": env._(
                    "The AI provider is not answering. Retry in a moment."
                ),
                "unreachable": env._(
                    "The panel could not reach the AI provider. Check that this server "
                    "can open the provider's address."
                ),
                "context_too_long": env._(
                    "This conversation is too long, even summarized. Start a new one."
                ),
            }
            scrubber = env["incubacloud.mcp.secrets"]._scrubber()
            text = texts.get(error.kind) or env._(
                "The AI provider refused the request: %s",
                scrubber.text(error.message)[:500],
            )
            conv._halt("error", text, error=True)

    # ── After the answer ─────────────────────────────────────────────────

    @api.model
    def _finish(self, conversation_id, state, text, error=False):
        """End the turn in *state*, with *text* for the person (or nothing)."""
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if conv:
                conv._halt(state, text, error=error)

    @api.model
    def _next(self, conversation_id):
        """Queue the next step of a task that goes on."""
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if conv and conv.state == "working":
                conv._enqueue_step()

    @api.model
    def _refuse_calls(self, conversation_id, calls, text):
        """Answer *calls* with *text*: they will not run."""
        if not calls:
            return
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            for call in calls if conv else ():
                conv._answer_call(call.id, call.name, text, is_error=True)

    @api.model
    def _progress(self, conversation_id, name):
        """Tell the person's panels which tool runs now."""
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if conv:
                label = env["incubacloud.assistant.message"]._label(name)
                conv._notify({"kind": "progress", "label": f"{label}…"})

    @api.model
    def _stop_requested(self, conversation_id):
        """Return whether the person pressed Stop (or the conversation is gone)."""
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            return not conv or conv.stop_requested

    @api.model
    def _run_calls(self, conversation_id, calls):
        """Run the calls in order, then queue the next step, or stop where it waits.

        A card leaves the calls after it open: they run once the person
        answers. A job answers the calls after it with "not run": the
        model asks again once it ends.
        """
        for index, call in enumerate(calls):
            if self._stop_requested(conversation_id):
                with self._durable() as env:
                    conv = self._conversation_in(env, conversation_id)
                    if conv:
                        conv._close_open_calls("Not run: the person stopped the task.")
                        conv._halt(
                            "stopped", env._("Stopped. What was done stays as it is.")
                        )
                return
            self._progress(conversation_id, call.name)
            outcome = self._execute(conversation_id, call)
            if outcome == "job":
                self._refuse_calls(
                    conversation_id,
                    calls[index + 1 :],
                    "Not run: the task waits for the job first. Ask again once it "
                    "ends.",
                )
            if outcome in ("card", "job", "gone"):
                return
        self._next(conversation_id)

    @api.model
    def _execute(self, conversation_id, call):
        """Run one call, then record what it answered; return what the task does next.

        ``"next"``: answered; ``"card"``: a card waits; ``"job"``: the task
        waits for a job; ``"gone"``: the conversation was deleted.

        First the call is marked as running, on a cursor of its own
        (``running_call_id``): a worker that dies between the tool and its
        answer leaves the mark, and the step that finds it answers "outcome
        unknown" instead of running the tool again. The tool runs in a
        transaction of its own that writes nothing of the conversation, so
        the person writing or pressing Stop meanwhile does not make it fail;
        a concurrency error there is never retried (the tool may have acted
        outside the database: a command, a commit) and the model is told.
        The mark and the answer only write the database: those are tried
        again (:meth:`_retried`).
        """
        started = self._retried(lambda: self._mark_running(conversation_id, call))
        if started == "gone":
            return "gone"
        if started == "unknown":
            outcome = {
                "text": "Outcome unknown: the panel stopped while this ran. Read the "
                "state again before trying once more.",
                "is_error": True,
            }
            return self._retried(
                lambda: self._record_call(conversation_id, call, outcome)
            )
        try:
            with self._durable() as env:
                outcome = env["incubacloud.assistant.engine"]._run_tool(
                    conversation_id, call
                )
        except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
            _logger.warning(
                "assistant: %s of conversation %s met a concurrent change",
                call.name,
                conversation_id,
            )
            outcome = {
                "text": "Not confirmed: the panel met a change made at the same time "
                "and undid what it wrote. Read the state again before trying once "
                "more: something outside the panel may have run.",
                "is_error": True,
            }
        return self._retried(lambda: self._record_call(conversation_id, call, outcome))

    @api.model
    def _mark_running(self, conversation_id, call):
        """Mark *call* as running; return ``"run"``, ``"unknown"`` or ``"gone"``.

        ``"unknown"``: the mark of this very call is there already, left by a
        step that died while it ran.
        """
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if not conv:
                return "gone"
            if conv.running_call_id == call.id:
                return "unknown"
            conv.sudo().running_call_id = call.id
            return "run"

    @api.model
    def _retried(self, write):
        """Run *write*, which only writes the database, again after a concurrency
        error: the person wrote or pressed Stop at the same time.

        :param write: a function of no arguments, with a cursor of its own
        """
        for attempt in range(WRITE_TRIES):
            try:
                return write()
            except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                if attempt == WRITE_TRIES - 1:
                    raise
                _sleep(0.2 * (attempt + 1))
        return None

    @api.model
    def _run_tool(self, conversation_id, call):
        """Run *call* in this environment's transaction; return what it answered.

        The assistant's own tools run here; the MCP's run through
        ``_tools_call`` inside a request of the user's own, as an agent's
        call does: role, arguments, secrets, confirmation, savepoint, audit.
        Nothing of the conversation is written here.

        :return: ``{"gone": True}``, ``{"card": {approval_id, message,
            production}}`` or ``{"text", "is_error", "job_id", "tainted"}``
        """
        if not self._conversation_in(self.env, conversation_id):
            return {"gone": True}
        Tools = self.env["incubacloud.assistant.tools"]
        local = Tools._local_spec(call.name)
        if local:
            if not Tools._allowed(local):
                return {
                    "text": "The person's role does not allow this tool.",
                    "is_error": True,
                }
            text, is_error = Tools._run_local(local, call.arguments)
            return {"text": text, "is_error": is_error}
        spec = self.env["incubacloud.mcp.catalog"]._spec(call.name)
        if spec is None or not Tools._allowed(spec):
            return {
                "text": f"Unknown tool, or one the person's role does not allow: "
                f"{call.name}.",
                "is_error": True,
            }
        ctx = call_context(self.env.uid, conversation_id)
        with tool_request(self.env, ctx.via()):
            try:
                result = self.env["incubacloud.mcp.server"]._tools_call(
                    {"name": call.name, "arguments": call.arguments}, ctx
                )
            except McpError as error:
                result = {
                    "content": [{"type": "text", "text": error.message}],
                    "isError": True,
                }
        if isinstance(result, dict) and result.get("assistant_confirmation"):
            return {
                "card": {
                    "approval_id": result["assistant_confirmation"],
                    "message": result.get("message") or "",
                    "production": Tools._acts_on_production(spec, call.arguments),
                }
            }
        text = "\n".join(
            part.get("text") or ""
            for part in result.get("content") or []
            if isinstance(part, dict)
        )
        is_error = bool(result.get("isError"))
        return {
            "text": text,
            "is_error": is_error,
            "job_id": self._job_of(spec, text, is_error).id,
            # Logs, files, mail, a command's output: others write them.
            "tainted": (spec.untrusted or spec.name in THIRD_PARTY_READS)
            and not is_error,
        }

    @api.model
    def _record_call(self, conversation_id, call, outcome):
        """Record what a call answered, on its own cursor; return what comes next."""
        with self._durable() as env:
            conv = self._conversation_in(env, conversation_id)
            if not conv or outcome.get("gone"):
                return "gone"
            conv.sudo().running_call_id = False
            card = outcome.get("card")
            if card:
                conv._add_message(
                    {
                        "role": "card",
                        "content": card["message"],
                        "approval_id": card["approval_id"],
                        "card_state": "pending",
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "production": card["production"],
                    }
                )
                conv._halt("waiting_confirmation", "")
                return "card"
            conv._answer_call(call.id, call.name, outcome["text"], outcome["is_error"])
            if outcome.get("tainted"):
                conv.sudo().tainted = True
            job = env["cloud.job"].sudo().browse(outcome.get("job_id") or []).exists()
            if job and job.state not in job._terminal_states:
                conv.sudo().waiting_job_id = job.id
                conv._halt("waiting_job", "", unread=False)
                return "job"
            return "next"

    @api.model
    def _job_of(self, spec, text, is_error):
        """Return the job a tool that enqueues answered, or an empty recordset."""
        Job = self.env["cloud.job"].sudo()
        if is_error or not spec.enqueues:
            return Job
        try:
            data = json.loads(text)
        except ValueError:
            return Job
        job_id = data.get("job_id") if isinstance(data, dict) else None
        if not isinstance(job_id, int) or isinstance(job_id, bool):
            return Job
        return Job.browse(job_id).exists()

    @api.model
    def _job_ended(self, conversation, job, state):
        """A job the task waited for ended: tell the model how, and go on.

        Called from ``cloud.job._after_job_terminal``, in the transaction that
        records the end, by whoever recorded it: the conversation and the
        job's log are read as the conversation's owner.
        """
        conv = conversation.sudo()
        owner = conv.user_id
        conv = conv.with_user(owner).with_context(lang=conv.lang or owner.lang)
        conv._add_message(
            {"role": "job", "content": self._job_outcome(job.with_user(owner), state)}
        )
        # A job's log is written by the code it ran: content others write.
        conv.sudo().write({"waiting_job_id": False, "tainted": True})
        conv._enqueue_step()

    @api.model
    def _job_outcome(self, job, state):
        """Return how *job* ended, for the model: state, error, its log's last lines."""
        if not job:
            return "The job the task waited for is gone: read the state with the tools."
        sudo = job.sudo()
        lines = [f"Job {job.id} ({sudo.name}) ended: {state}."]
        if state == "failed":
            error = sudo._failure_excerpt(sudo.queue_job_id.exc_message)
            if error:
                lines.append(f"Error: {error}")
        chunks = job.env["cloud.job.log.chunk"].search(
            [("job_id", "=", job.id)], order="id desc", limit=5
        )
        tail = "\n".join(chunk.content or "" for chunk in reversed(chunks))[
            -JOB_LOG_CHARS:
        ]
        if tail:
            lines += ["Last lines of its log:", tail]
        text = "\n".join(lines)
        return job.env["incubacloud.mcp.secrets"]._scrubber().text(text)

    @api.model
    def _summarize(self, conversation_id, connector, model, config):
        """Summarize the start of a conversation too long; completed in task 16."""
        return False
