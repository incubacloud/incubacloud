"""OpenAI and OpenRouter: the Chat Completions API, at fixed addresses."""

import json
from typing import ClassVar

from ._http import call_json, kind_of, retry_after
from .base import (
    ChatResponse,
    Connector,
    ModelInfo,
    ProviderError,
    ToolCall,
    Usage,
    parse_arguments,
    register,
)


def _per_million(value):
    """Return a price per token (text) as dollars per million tokens, or None."""
    try:
        return round(float(value) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


class ChatCompletionsConnector(Connector):
    """What OpenAI and OpenRouter share."""

    base_url = ""
    #: How the request names its cap of output tokens.
    max_tokens_key = "max_tokens"

    def _headers(self):
        """Return the request's headers, with the key."""
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    def _model(self, item):
        """Return a listing entry as a :class:`ModelInfo`, or None to leave it out."""
        raise NotImplementedError

    def list_models(self):
        """Return the models of the listing the assistant can use."""
        status, headers, data = call_json(
            "GET", f"{self.base_url}/models", self._headers()
        )
        if status != 200:
            raise self._error(status, headers, data)
        return [info for item in data.get("data") or [] if (info := self._model(item))]

    def chat(self, request):
        """Send *request* to ``/chat/completions``."""
        payload = {
            "model": request.model,
            "messages": self._messages(request),
            self.max_tokens_key: request.max_tokens,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.schema,
                    },
                }
                for tool in request.tools
            ]
        status, headers, data = call_json(
            "POST", f"{self.base_url}/chat/completions", self._headers(), payload
        )
        if status != 200 or isinstance(data.get("error"), dict):
            raise self._error(status, headers, data)
        return self._response(data, request.model)

    def _messages(self, request):
        """Return the conversation as Chat Completions ``messages``."""
        messages = [{"role": "system", "content": request.system}]
        for turn in request.turns:
            if turn.role == "assistant" and not (turn.text or turn.tool_calls):
                # An empty answer of the model: the API refuses it sent back.
                continue
            if turn.role == "assistant":
                message = {"role": "assistant", "content": turn.text or None}
                if turn.tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(
                                    call.arguments, ensure_ascii=False
                                ),
                            },
                        }
                        for call in turn.tool_calls
                    ]
                messages.append(message)
            elif turn.role == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": turn.tool_call_id,
                        "content": turn.text,
                    }
                )
            else:
                messages.append({"role": "user", "content": turn.text})
        return messages

    def _response(self, data, requested):
        """Return the first choice as a :class:`ChatResponse`.

        :raises ProviderError: for a choice that ended in an error
        """
        choice = (data.get("choices") or [{}])[0]
        usage = self._usage(data)
        if choice.get("finish_reason") == "error" or choice.get("error"):
            error = choice.get("error")
            error = error if isinstance(error, dict) else {}
            raise self._failure(
                200,
                {"message": "The model stopped with an error.", **error},
                usage=usage if usage.input_tokens or usage.output_tokens else None,
                model=data.get("model") or "",
            )
        message = choice.get("message") or {}
        calls = tuple(
            ToolCall(
                call.get("id") or f"call_{index}",
                (call.get("function") or {}).get("name") or "",
                parse_arguments((call.get("function") or {}).get("arguments")),
            )
            for index, call in enumerate(message.get("tool_calls") or [])
        )
        stop = {
            "tool_calls": "tool_use",
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(choice.get("finish_reason"), "tool_use" if calls else "end")
        return ChatResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            usage=usage,
            model=data.get("model") or requested,
            stop=stop,
        )

    @staticmethod
    def _usage(data):
        """Return the tokens an answer says it used.

        Cached and cache-written tokens apart from the rest. A provider that
        does not split them (OpenRouter for some models) leaves them all as
        input: priced in full, never below what was billed.
        """
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") or 0
        written = details.get("cache_write_tokens") or 0
        return Usage(
            input_tokens=max(0, (usage.get("prompt_tokens") or 0) - cached - written),
            output_tokens=usage.get("completion_tokens") or 0,
            cache_read_tokens=cached,
            cache_write_tokens=written,
        )

    #: Error codes and types of the API, by what they mean here.
    _CODES: ClassVar[dict] = {
        "insufficient_quota": "quota",
        "credit_balance_exhausted": "quota",
        "billing_hard_limit_reached": "quota",
        "billing_not_active": "quota",
        "context_length_exceeded": "context_too_long",
        "invalid_api_key": "invalid_key",
    }
    #: Words of a refusal that mean a spending limit rather than a refusal.
    _LIMIT_WORDS = ("limit", "credit", "budget", "quota")
    #: Whether an HTTP 403 is a limit or a guardrail rather than the key.
    forbidden_is_a_limit = False

    def _failure(self, status, error, retry=None, usage=None, model=""):
        """Return the failure an error object describes, as a :class:`ProviderError`.

        By its code or type first (a 429 that says the account has no
        credit is no rate limit), then by its status: the HTTP one or,
        inside a 200, the error's own numeric code. A 403 inside an answer
        is never the key, which was taken: a spending limit or a refusal.
        An error inside a 200 without a code is an outage.
        """
        message = str(error.get("message") or f"HTTP {status}")
        code = error.get("code")
        inside = status == 200
        kind = self._CODES.get(code) or self._CODES.get(error.get("type"))
        if kind is None and inside and not isinstance(code, int):
            kind = "unavailable"
        if kind is None:
            effective = code if inside else status
            if effective == 403 and (inside or self.forbidden_is_a_limit):
                limited = any(word in message.lower() for word in self._LIMIT_WORDS)
                kind = "quota" if limited else "bad_request"
            else:
                kind = kind_of(effective, message)
        if kind == "invalid_key":
            message = "The provider refused the key."
        return ProviderError(kind, message, retry, usage=usage, model=model)

    def _error(self, status, headers, data):
        """Return a refusal, an HTTP one or an error inside a 200."""
        error = data.get("error")
        error = error if isinstance(error, dict) else {"message": str(error or "")}
        usage = self._usage(data) if data.get("usage") else None
        return self._failure(
            status, error, retry_after(headers), usage, data.get("model") or ""
        )


@register
class OpenAIConnector(ChatCompletionsConnector):
    """OpenAI (beta in the panel)."""

    provider = "openai"
    label = "OpenAI"
    base_url = "https://api.openai.com/v1"
    max_tokens_key = "max_completion_tokens"
    #: Words of the ids of models that do not chat with tools.
    _SKIP = (
        "embedding",
        "whisper",
        "tts",
        "dall-e",
        "davinci",
        "babbage",
        "moderation",
        "audio",
        "realtime",
        "transcribe",
        "image",
        "search",
        "sora",
    )

    def _model(self, item):
        """Chat models only: ``gpt-``, ``o1``, ``o3``, ``o4``, ``chatgpt-``."""
        model_id = str(item.get("id") or "")
        if not model_id.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
            return None
        if any(word in model_id for word in self._SKIP):
            return None
        return ModelInfo(model_id, model_id)


@register
class OpenRouterConnector(ChatCompletionsConnector):
    """OpenRouter (beta in the panel); its listing says which models take tools.

    Each request names one model, the one the manager allowed: never the
    ``models`` list of OpenRouter's fallbacks nor a router (``openrouter/…``)
    that picks another model; the engine refuses an answer of any other model.
    """

    provider = "openrouter"
    label = "OpenRouter"
    base_url = "https://openrouter.ai/api/v1"

    def _headers(self):
        """OpenRouter's headers, with who the app is."""
        return {
            **super()._headers(),
            "HTTP-Referer": "https://www.incubacloud.io",
            "X-Title": "IncubaCloud panel",
            "X-OpenRouter-Title": "IncubaCloud panel",
        }

    def list_models(self):
        """Try the key first: OpenRouter lists its models to anybody, keyless."""
        url = f"{self.base_url}/key"
        status, headers, data = call_json("GET", url, self._headers())
        if status != 200:
            raise self._error(status, headers, data)
        return super().list_models()

    #: OpenRouter's 403 is a limit of the key or a guardrail, never the key.
    forbidden_is_a_limit = True

    def _model(self, item):
        """Models that take tools, with their prices per million tokens; no router."""
        model_id = str(item.get("id") or "")
        if model_id.startswith("openrouter/"):
            return None
        if "tools" not in (item.get("supported_parameters") or []):
            return None
        pricing = item.get("pricing") or {}
        price_in = _per_million(pricing.get("prompt"))
        price_out = _per_million(pricing.get("completion"))
        if (price_in or 0) < 0 or (price_out or 0) < 0:
            # -1: a router, whose price is the model it picks.
            return None
        return ModelInfo(
            model_id, item.get("name") or model_id, True, price_in, price_out
        )
