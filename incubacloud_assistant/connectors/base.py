"""What every provider's connector takes and gives back: one shape for the engine.

A conversation is a tuple of :class:`Turn`: the person's (``user``), the
model's (``assistant``: its text, the tools it asked for and, to send back
unchanged, the provider's own content) and the tools' answers (``tool``).
A connector turns it into its provider's request, and the answer into a
:class:`ChatResponse`; every failure is a :class:`ProviderError` of a
known kind. No ORM here.
"""

import json
import re
from dataclasses import asdict, dataclass

#: What may follow a model's id in the id of a dated version of the same
#: model: ``-20251001``, ``-2026-08-07``, ``-001``, ``-0613``, ``-05-06``.
_SNAPSHOT_RE = re.compile(r"\d{8}|\d{4}-\d{2}-\d{2}|\d{3,4}|\d{2}-\d{2}")
#: The kinds of failure the engine tells apart.
ERROR_KINDS = (
    "invalid_key",
    "quota",
    "rate_limited",
    "unavailable",
    "unreachable",
    "context_too_long",
    "bad_request",
)
#: The kinds worth trying again after a pause.
RETRYABLE = ("rate_limited", "unavailable")
#: ``{provider code: connector class}``, filled by :func:`register`.
REGISTRY = {}


def register(cls):
    """Class decorator: make *cls* the connector of its ``provider``."""
    REGISTRY[cls.provider] = cls
    return cls


@dataclass(frozen=True)
class ToolCall:
    """A tool the model asked for, with its decoded arguments."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Turn:
    """One message of the conversation, as every connector reads it."""

    role: str
    text: str = ""
    tool_calls: tuple = ()
    tool_call_id: str = ""
    tool_name: str = ""
    is_error: bool = False
    raw: object = None
    raw_provider: str = ""


@dataclass(frozen=True)
class ToolDef:
    """A tool offered to the model: name, description, JSON Schema."""

    name: str
    description: str
    schema: dict


@dataclass(frozen=True)
class ChatRequest:
    """What the engine asks a provider for."""

    model: str
    system: str
    tools: tuple
    turns: tuple
    max_tokens: int = 16000


@dataclass(frozen=True)
class Usage:
    """Tokens of one call: input (not cached), output, cache read, cache written."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other):
        """Return the sum of two usages."""
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class ChatResponse:
    """A provider's answer.

    ``stop``: ``end``, ``tool_use``, ``max_tokens`` or ``refusal``.
    ``model``: the model the provider says answered (``""`` when it does
    not say); the engine refuses an answer of another model than the one
    asked for (:func:`same_model`).
    """

    text: str
    tool_calls: tuple
    usage: Usage
    model: str
    stop: str
    raw: object = None


@dataclass(frozen=True)
class ModelInfo:
    """A model of the provider's listing.

    ``tools``: whether it takes tools, None when the provider does not say.
    Prices in US dollars per million tokens, when the listing gives them.
    """

    id: str
    name: str
    tools: object = None
    price_in: object = None
    price_out: object = None

    def as_dict(self):
        """Return the entry as the settings keep it."""
        return asdict(self)


class ProviderError(Exception):
    """A provider's failure, of one of :data:`ERROR_KINDS`.

    ``usage`` and ``model``: what a failure the provider still billed used
    (an answer that ended in an error after it was generated), or None.
    """

    def __init__(
        self, kind, message, retry_after=None, usage=None, model="", ours=False
    ):
        """Keep the kind (``bad_request`` when unknown), the message, the pause
        and, for a billed failure, its tokens and the model that ran.

        :param float retry_after: seconds the provider asked to wait, or None
        :param Usage usage: the tokens the provider billed, or None
        :param bool ours: the panel refused before asking the provider; the
            message is ours, in English, and the person reads a text of
            their language instead
        """
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "bad_request"
        self.message = message
        self.retry_after = retry_after
        self.usage = usage
        self.model = model
        self.ours = ours

    def billed(self, requested):
        """Return the billed failure as an answer without content, to price it."""
        return ChatResponse(
            text="",
            tool_calls=(),
            usage=self.usage or Usage(),
            model=self.model or requested,
            stop="end",
        )

    @property
    def retryable(self):
        """Whether trying again after a pause may work."""
        return self.kind in RETRYABLE


class Connector:
    """A provider's connector. Subclasses set ``provider`` and register."""

    provider = ""
    label = ""

    def __init__(self, api_key):
        """Keep the key, for this connector's requests only."""
        self._key = api_key

    def list_models(self):
        """Return the provider's models usable with this key.

        :rtype: list[ModelInfo]
        :raises ProviderError: when the provider refuses or cannot be reached
        """
        raise NotImplementedError

    def chat(self, request):
        """Send *request*; return the provider's answer.

        :param ChatRequest request: the conversation and the tools
        :rtype: ChatResponse
        :raises ProviderError: when the provider refuses or cannot be reached
        """
        raise NotImplementedError


def parse_arguments(raw):
    """Return the tool arguments in *raw* (JSON text or a dict).

    Text that is not a JSON object is kept under ``_unparsed``: the tool's
    validator refuses it and the model reads why.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {"_unparsed": str(raw)}
    return value if isinstance(value, dict) else {"_unparsed": str(raw)}


def same_model(requested, answered):
    """Return whether *answered* is the model asked for.

    The same id, or the id of a dated version of it (providers answer the
    snapshot an alias points to). A provider that does not say which model
    answered (``""``) answered nothing different.
    """
    if not answered or answered == requested:
        return True
    prefix = f"{requested}-"
    return answered.startswith(prefix) and bool(
        _SNAPSHOT_RE.fullmatch(answered[len(prefix) :])
    )


def sides(turns):
    """Return the conversation as alternating sides.

    ``[("user", [turns]), ("assistant", [turn]), …]``: the tools' answers
    and the person's text after an answer of the model make one side, as
    providers with strict turns want it.
    """
    grouped = []
    for turn in turns:
        side = "assistant" if turn.role == "assistant" else "user"
        if grouped and grouped[-1][0] == side == "user":
            grouped[-1][1].append(turn)
        else:
            grouped.append((side, [turn]))
    return grouped
