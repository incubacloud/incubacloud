"""Anthropic's Messages API, over HTTP at its fixed address.

The API version is pinned (:data:`API_VERSION`), and so are the shapes this
connector sends and reads; the recorded answers of the tests
(``tests/recorded.py``) have them. Changing the version means changing the
constant, these shapes and those recordings, on purpose.

Request, ``POST /v1/messages``::

    {"model", "max_tokens",
     "system": [{"type": "text", "text", "cache_control": {"type": "ephemeral"}}],
     "tools": [{"name", "description", "input_schema"}],
     "messages": [{"role": "user" | "assistant", "content": [blocks]}],
     "cache_control": {"type": "ephemeral"},
     # BOUND_THINKING only, with anthropic-beta: thinking-binding-controls-2026-08-01
     "thinking": {"type": "adaptive",
                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
     # recommended models only
     "output_config": {"effort"}}

Answer: ``{"id", "type": "message", "role", "model", "content": [blocks],
"stop_reason", "usage": {"input_tokens", "output_tokens",
"cache_creation_input_tokens", "cache_read_input_tokens"}}``, with blocks
``text`` (``text``), ``thinking`` (``thinking``, ``signature``),
``redacted_thinking`` (``data``) and ``tool_use`` (``id``, ``name``,
``input``); ``tool_result`` blocks go back in the person's turn. Errors:
``{"type": "error", "error": {"type", "message"}}`` (402: ``billing_error``). Models,
``GET /v1/models?limit=1000[&after_id=…]``: ``{"data": [{"id",
"display_name"}], "has_more", "last_id"}``.

Not streamed: each step is a queue job of its own, and one answer of
:data:`MAX_TOKENS` at most comes in one response. No ``fallbacks``: the
request names the model the manager allowed, and the engine refuses an
answer of any other model.
"""

import urllib.parse

from ._http import call_json, kind_of, retry_after
from .base import (
    ChatResponse,
    Connector,
    ModelInfo,
    ProviderError,
    ToolCall,
    Usage,
    register,
    same_model,
    sides,
)

BASE_URL = "https://api.anthropic.com"
#: The Messages API version every request names.
API_VERSION = "2023-06-01"
#: The beta the request of a model of :data:`BOUND_THINKING` names: a thinking
#: block whose conversation changed (a summary, tools changed with the role)
#: is dropped instead of refused.
BETAS = ("thinking-binding-controls-2026-08-01",)
#: The models that check a replayed thinking block against the conversation
#: before it (and their dated versions), recommended or not.
BOUND_THINKING = (
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
)
#: Output tokens of one answer at most, thinking included.
MAX_TOKENS = 16000
#: Effort of each recommended model (QA walk 29 sets them).
EFFORT = {"claude-opus-5-5": "medium", "claude-sonnet-5-5": "medium"}
#: Pages of the model listing read at most.
_MAX_PAGES = 5
#: Error types of the API, by what they mean here.
_KINDS = {
    "authentication_error": "invalid_key",
    "permission_error": "invalid_key",
    "billing_error": "quota",
    "rate_limit_error": "rate_limited",
    "overloaded_error": "unavailable",
    "api_error": "unavailable",
    "request_too_large": "context_too_long",
    "not_found_error": "bad_request",
}


@register
class AnthropicConnector(Connector):
    """Anthropic's Messages API."""

    provider = "anthropic"
    label = "Anthropic"

    def _headers(self, betas=()):
        """Return the request's headers: the key, the pinned version, the betas."""
        headers = {
            "x-api-key": self._key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        return headers

    def list_models(self):
        """Return every model of the account, page by page; Claude models take tools."""
        models, after = [], ""
        for _page in range(_MAX_PAGES):
            url = f"{BASE_URL}/v1/models?limit=1000"
            if after:
                url += f"&after_id={urllib.parse.quote(after, safe='')}"
            status, headers, data = call_json("GET", url, self._headers())
            if status != 200:
                raise self._error(status, headers, data)
            for item in data.get("data") or []:
                model_id = str(item.get("id") or "")
                if model_id:
                    models.append(
                        ModelInfo(model_id, item.get("display_name") or model_id, True)
                    )
            after = str(data.get("last_id") or "") if data.get("has_more") else ""
            if not after:
                break
        return models

    def chat(self, request):
        """Send *request* to ``/v1/messages`` and return the answer."""
        payload = {
            "model": request.model,
            "max_tokens": min(request.max_tokens or MAX_TOKENS, MAX_TOKENS),
            "system": [
                {
                    "type": "text",
                    "text": request.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": self._messages(request.turns),
            "cache_control": {"type": "ephemeral"},
        }
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.schema,
                }
                for tool in request.tools
            ]
        betas = ()
        if any(same_model(model, request.model) for model in BOUND_THINKING):
            payload["thinking"] = {
                "type": "adaptive",
                "block_binding": {"prefix_mismatch_behavior": "drop_block"},
            }
            betas = BETAS
        if request.model in EFFORT:
            payload["output_config"] = {"effort": EFFORT[request.model]}
        url = f"{BASE_URL}/v1/messages"
        status, headers, data = call_json("POST", url, self._headers(betas), payload)
        if status == 400 and _thinking_refused(data):
            # A newer model that checks its thinking blocks: once more, without.
            # The last resort: if this fails too, its answer is the error.
            payload["messages"] = _without_thinking(payload["messages"])
            status, headers, data = call_json(
                "POST", url, self._headers(betas), payload
            )
        if status != 200:
            raise self._error(status, headers, data)
        return self._response(data)

    def _messages(self, turns):
        """Return the conversation as Anthropic's ``messages``."""
        messages = []
        for side, group in sides(turns):
            if side == "assistant":
                messages.append(
                    {"role": "assistant", "content": self._assistant(group[0])}
                )
                continue
            content = []
            for turn in group:
                if turn.role == "tool":
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": turn.tool_call_id,
                            "content": turn.text or "(empty)",
                            "is_error": turn.is_error,
                        }
                    )
                elif turn.text:
                    content.append({"type": "text", "text": turn.text})
            messages.append({"role": "user", "content": content})
        return messages

    def _assistant(self, turn):
        """Return the content of one answer of the model.

        Its own blocks when it was Anthropic's, as they came; otherwise its
        text and its tool calls.
        """
        if turn.raw_provider == self.provider and turn.raw:
            return turn.raw
        content = [{"type": "text", "text": turn.text}] if turn.text else []
        content += [
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
            for call in turn.tool_calls
        ]
        return content or [{"type": "text", "text": "(no answer)"}]

    @staticmethod
    def _response(data):
        """Return an answer of ``/v1/messages`` as a :class:`ChatResponse`.

        :raises ProviderError: ``unavailable`` for a body that is no message
        """
        if not isinstance(data.get("content"), list):
            raise ProviderError("unavailable", "The provider's answer has no content.")
        blocks = [block for block in data["content"] if isinstance(block, dict)]
        usage = data.get("usage") or {}
        return ChatResponse(
            text="".join(
                b.get("text") or "" for b in blocks if b.get("type") == "text"
            ),
            tool_calls=tuple(
                ToolCall(b.get("id") or "", b.get("name") or "", b.get("input") or {})
                for b in blocks
                if b.get("type") == "tool_use"
            ),
            usage=Usage(
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
                cache_write_tokens=usage.get("cache_creation_input_tokens") or 0,
            ),
            model=str(data.get("model") or ""),
            stop={
                "tool_use": "tool_use",
                "max_tokens": "max_tokens",
                "refusal": "refusal",
            }.get(data.get("stop_reason"), "end"),
            raw=blocks,
        )

    @staticmethod
    def _error(status, headers, data):
        """Return a refusal as a :class:`ProviderError`, without the key."""
        error = data.get("error") if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        message = str(error.get("message") or f"HTTP {status}")
        kind = _KINDS.get(error.get("type"))
        if kind is None:
            lowered = message.lower()
            if "credit balance" in lowered:
                kind = "quota"
            elif "too long" in lowered:
                kind = "context_too_long"
            else:
                kind = kind_of(status, message)
        if kind == "invalid_key":
            message = "The provider refused the key."
        return ProviderError(kind, message, retry_after(headers))


def _thinking_refused(data):
    """Return whether *data* refuses a replayed thinking block (its signature)."""
    error = data.get("error") if isinstance(data, dict) else None
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    return "signature" in message.lower() and "thinking" in message.lower()


def _without_thinking(messages):
    """Return *messages* without the model's thinking blocks."""
    kept = []
    for message in messages:
        content = message["content"]
        if message["role"] == "assistant" and isinstance(content, list):
            content = [
                block
                for block in content
                if block.get("type") not in ("thinking", "redacted_thinking")
            ] or [{"type": "text", "text": "(no answer)"}]
        kept.append({**message, "content": content})
    return kept
