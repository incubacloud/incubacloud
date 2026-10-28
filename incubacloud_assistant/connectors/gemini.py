"""Google Gemini: ``generateContent`` at its fixed address.

The key goes in the ``x-goog-api-key`` header, never in the URL. Gemini's
function declarations take a subset of OpenAPI schemas: no
``additionalProperties``, and an object needs its properties; a free-form
object (an update's ``vals``) is declared as JSON text and decoded back
before the tool sees it, and a tool without arguments is declared without
``parameters``. A call carries its ``id`` back in its response (Gemini 3
wants it); a model's turn rebuilt from another provider's answer has no
thought signature of Gemini's, and says so.
"""

import json
import re
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
    sides,
)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
#: Schema keys Gemini's declarations accept.
_KEPT = (
    "type",
    "description",
    "enum",
    "properties",
    "required",
    "items",
    "minimum",
    "maximum",
    "maxLength",
    "format",
    "nullable",
)
#: Pages of the listing read at most.
_MAX_PAGES = 5
#: Words of the ids of models that do not chat with tools.
_SKIP = ("embedding", "tts", "image", "audio", "live", "aqa")
#: The ids this connector gives calls that came without one.
_OWN_ID = "gemini_call_"
#: What a call rebuilt from another provider's answer carries instead of a
#: thought signature of Gemini's.
_NO_SIGNATURE = "skip_thought_signature_validator"
#: Why a candidate ended, when it is a refusal.
_REFUSED = (
    "SAFETY",
    "RECITATION",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "IMAGE_SAFETY",
    "LANGUAGE",
)
#: Why a candidate ended, when trying again may work.
_FAILED = (
    "MALFORMED_FUNCTION_CALL",
    "UNEXPECTED_TOOL_CALL",
    "TOO_MANY_TOOL_CALLS",
    "MISSING_THOUGHT_SIGNATURE",
    "MALFORMED_RESPONSE",
    "OTHER",
)
#: Why a candidate ended, when the answer goes on past this one.
_CUT = ("MAX_TOKENS", "CONTINUATION")
#: ``RetryInfo.retryDelay``: ``"7s"``.
_DELAY_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")


def _free_object(schema):
    """Return whether *schema* is an open object with no declared properties."""
    return (
        schema.get("type") == "object"
        and not schema.get("properties")
        and schema.get("additionalProperties") is not False
    )


def gemini_schema(schema):
    """Return *schema* as Gemini's declarations take it.

    An object with properties and room for more keeps only the declared
    ones: Gemini has no ``additionalProperties``.
    """
    if _free_object(schema):
        description = schema.get("description") or ""
        return {
            "type": "string",
            "description": f"{description} A JSON object, as text.".strip(),
        }
    converted = {key: value for key, value in schema.items() if key in _KEPT}
    if "properties" in converted:
        converted["properties"] = {
            name: gemini_schema(item) for name, item in converted["properties"].items()
        }
    if "items" in converted:
        converted["items"] = gemini_schema(converted["items"])
    return converted


def _decode_free(schema, args):
    """Decode the JSON text of free-form objects in *args* back into objects."""
    properties = schema.get("properties") or {}
    decoded = dict(args)
    for name, item in properties.items():
        value = decoded.get(name)
        if _free_object(item) and isinstance(value, str):
            try:
                decoded[name] = json.loads(value)
            except ValueError:
                decoded[name] = {"_unparsed": value}
    return decoded


@register
class GeminiConnector(Connector):
    """Google Gemini (beta in the panel)."""

    provider = "google"
    label = "Google Gemini"

    def _headers(self):
        """The request's headers, with the key."""
        return {"x-goog-api-key": self._key, "Content-Type": "application/json"}

    def list_models(self):
        """Return the models that generate content, page by page, aliases left out."""
        models, token = [], ""
        for _page in range(_MAX_PAGES):
            url = f"{BASE_URL}/models?pageSize=1000"
            if token:
                url += f"&pageToken={urllib.parse.quote(token, safe='')}"
            status, headers, data = call_json("GET", url, self._headers())
            if status != 200:
                raise self._error(status, headers, data)
            for item in data.get("models") or []:
                if "generateContent" not in (
                    item.get("supportedGenerationMethods") or []
                ):
                    continue
                model_id = str(item.get("name") or "").removeprefix("models/")
                # An alias answers with the model it points to today: another
                # name, which the engine refuses. The manager picks a model.
                if model_id.endswith("latest") or any(w in model_id for w in _SKIP):
                    continue
                models.append(ModelInfo(model_id, item.get("displayName") or model_id))
            token = data.get("nextPageToken") or ""
            if not token:
                break
        return models

    def chat(self, request):
        """Send *request* to ``generateContent``.

        :raises ProviderError: ``bad_request`` for a model id that would
            change the URL's path
        """
        if not request.model or any(char in request.model for char in "/?#%"):
            raise ProviderError("bad_request", "Unknown model.")
        self._schemas = {tool.name: tool.schema for tool in request.tools}
        payload = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": self._contents(request.turns),
            "generationConfig": {"maxOutputTokens": request.max_tokens},
        }
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        self._declare(tool) for tool in request.tools
                    ]
                }
            ]
        status, headers, data = call_json(
            "POST",
            f"{BASE_URL}/models/{request.model}:generateContent",
            self._headers(),
            payload,
        )
        if status != 200:
            raise self._error(status, headers, data)
        return self._response(data, request.model)

    @staticmethod
    def _declare(tool):
        """Return the function declaration of *tool*; no ``parameters`` without any."""
        declaration = {"name": tool.name, "description": tool.description}
        if tool.schema.get("properties") or _free_object(tool.schema):
            declaration["parameters"] = gemini_schema(tool.schema)
        return declaration

    def _contents(self, turns):
        """Return the conversation as Gemini's ``contents``."""
        contents = []
        for side, group in sides(turns):
            if side == "assistant":
                turn = group[0]
                if turn.raw_provider == self.provider and turn.raw:
                    parts = turn.raw
                else:
                    parts = [{"text": turn.text}] if turn.text else []
                    parts += [
                        {
                            "functionCall": {
                                # This connector's own ids never went to Gemini.
                                **(
                                    {}
                                    if call.id.startswith(_OWN_ID)
                                    else {"id": call.id}
                                ),
                                "name": call.name,
                                "args": call.arguments,
                            },
                            "thoughtSignature": _NO_SIGNATURE,
                        }
                        for call in turn.tool_calls
                    ]
                contents.append({"role": "model", "parts": parts or [{"text": "…"}]})
                continue
            parts = []
            for turn in group:
                if turn.role == "tool":
                    answer = {
                        "name": turn.tool_name,
                        "response": {"error" if turn.is_error else "result": turn.text},
                    }
                    if turn.tool_call_id and not turn.tool_call_id.startswith(_OWN_ID):
                        answer["id"] = turn.tool_call_id
                    parts.append({"functionResponse": answer})
                elif turn.text:
                    parts.append({"text": turn.text})
            contents.append({"role": "user", "parts": parts})
        return contents

    def _response(self, data, requested):
        """Return the first candidate as a :class:`ChatResponse`.

        :raises ProviderError: ``unavailable`` when the candidate ended in a
            failure trying again may mend (a malformed call)
        """
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked and not data.get("candidates"):
            return ChatResponse(
                text="",
                tool_calls=(),
                usage=Usage(),
                model=data.get("modelVersion") or requested,
                stop="refusal",
            )
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(
            part.get("text") or ""
            for part in parts
            if "text" in part and not part.get("thought")
        )
        schemas = getattr(self, "_schemas", {})
        calls = tuple(
            ToolCall(
                part["functionCall"].get("id") or f"{_OWN_ID}{index}",
                part["functionCall"].get("name") or "",
                _decode_free(
                    schemas.get(part["functionCall"].get("name"), {}),
                    part["functionCall"].get("args") or {},
                ),
            )
            for index, part in enumerate(parts)
            if "functionCall" in part
        )
        finish = candidate.get("finishReason")
        usage = _usage(data.get("usageMetadata") or {})
        if finish in _FAILED:
            # Generated, and billed: the engine counts it.
            raise ProviderError(
                "unavailable",
                f"The model's answer ended in {finish}.",
                usage=usage if usage.input_tokens or usage.output_tokens else None,
                model=data.get("modelVersion") or "",
            )
        if finish in _REFUSED:
            stop = "refusal"
        elif finish in _CUT:
            stop = "max_tokens"
        else:
            stop = "tool_use" if calls else "end"
        return ChatResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            model=data.get("modelVersion") or requested,
            stop=stop,
            raw=parts,
        )

    def _error(self, status, headers, data):
        """Return a refusal as a :class:`ProviderError`, with the pause it asks."""
        error = data.get("error") if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        message = error.get("message") or f"HTTP {status}"
        denied = error.get("status") == "PERMISSION_DENIED"
        if "api key not valid" in message.lower() or denied:
            return ProviderError("invalid_key", "The provider refused the key.")
        return ProviderError(
            kind_of(status, message),
            message,
            _retry_delay(error) or retry_after(headers),
        )


def _usage(meta):
    """Return the tokens of Gemini's ``usageMetadata``; thoughts are output."""
    cached = meta.get("cachedContentTokenCount") or 0
    return Usage(
        input_tokens=max(0, (meta.get("promptTokenCount") or 0) - cached),
        output_tokens=(meta.get("candidatesTokenCount") or 0)
        + (meta.get("thoughtsTokenCount") or 0),
        cache_read_tokens=cached,
    )


def _retry_delay(error):
    """Return the seconds a ``RetryInfo`` of the error asks to wait (at most 60)."""
    for detail in error.get("details") or []:
        found = _DELAY_RE.match(str((detail or {}).get("retryDelay") or ""))
        if found:
            return min(60.0, float(found.group(1)))
    return None
