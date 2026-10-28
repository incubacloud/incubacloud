"""One JSON request to a provider's fixed address, with ``requests``.

No redirect is followed, the answer is read up to a cap, and a failure
says what kind it is without anything of the request in it.
"""

import json

import requests

from .base import ProviderError

#: Seconds to connect, and to wait between two reads: a long answer is
#: generated for minutes before its first byte.
TIMEOUT = (10, 600)
#: Largest answer read.
MAX_ANSWER_BYTES = 8 * 1024 * 1024
#: Words of a provider's refusal that mean the conversation is too long.
_CONTEXT_WORDS = (
    "context length",
    "context_length",
    "maximum context",
    "too long",
    "exceeds the maximum number of tokens",
    "input token count",
)


def call_json(method, url, headers, payload=None):
    """Send one request; return ``(status, headers, decoded JSON or {})``.

    :raises ProviderError: ``unreachable`` when no connection is made,
        ``unavailable`` when it times out or the answer is too large
    """
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except requests.Timeout as exc:
        raise ProviderError(
            "unavailable", "The provider did not answer in time."
        ) from exc
    except requests.RequestException as exc:
        raise ProviderError(
            "unreachable", "The panel could not reach the provider."
        ) from exc
    try:
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body += chunk
            if len(body) > MAX_ANSWER_BYTES:
                raise ProviderError(
                    "unavailable", "The provider's answer is too large."
                )
        try:
            data = json.loads(bytes(body)) if body else {}
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if 200 <= response.status_code < 300:
                # A success that is no JSON object is no answer of a model.
                raise ProviderError(
                    "unavailable", "The provider's answer could not be read."
                )
            data = {}
        return response.status_code, dict(response.headers or {}), data
    except requests.RequestException as exc:
        raise ProviderError("unavailable", "The provider's answer was cut.") from exc
    finally:
        response.close()


def retry_after(headers):
    """Return the seconds the provider asked to wait (at most 60), or None."""
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return min(60.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def kind_of(status, message):
    """Return the kind of a refusal with HTTP *status* and *message*."""
    text = (message or "").lower()
    if status in (401, 403):
        return "invalid_key"
    if status == 402:
        return "quota"
    if status == 429:
        return "rate_limited"
    if status == 408 or status >= 500:
        return "unavailable"
    if status in (400, 413) and any(word in text for word in _CONTEXT_WORDS):
        return "context_too_long"
    return "bad_request"
