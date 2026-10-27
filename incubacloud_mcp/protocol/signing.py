"""Self-contained, HMAC-signed tokens: the session id and ``requestState``.

The server keeps no memory between requests. What it must find again — a
legacy client's capabilities, the approval a confirmation refers to — is
handed to the client signed, and read back only if the signature, made
with the module's key and a label of its own, still holds.
"""

import base64
import binascii
import hashlib
import hmac
import json


def _b64encode(raw):
    """Return *raw* as unpadded URL-safe base64 text."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text):
    """Decode unpadded URL-safe base64 *text* into bytes."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key, label, body):
    """Return the signature of *body* under *key* and *label*."""
    return _b64encode(
        hmac.new(key, f"{label}.{body}".encode(), hashlib.sha256).digest()
    )


def sign(key, label, payload):
    """Return ``<body>.<signature>`` for the JSON *payload*.

    :param bytes key: the module's secret
    :param str label: what the token is for (``session``, ``request``)
    :param dict payload: JSON-serialisable content
    :rtype: str
    """
    body = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    return f"{body}.{_mac(key, label, body)}"


def verify(key, label, token):
    """Return the payload of *token*, or None when it does not verify.

    :param bytes key: the module's secret
    :param str label: the label it must have been signed with
    :param token: what the client sent back (attacker-controlled)
    :rtype: dict | None
    """
    if not isinstance(token, str) or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    # Compared as bytes: ``compare_digest`` refuses a non-ASCII str.
    if not hmac.compare_digest(mac.encode(), _mac(key, label, body).encode()):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def fingerprint(tool, arguments):
    """Return the digest that binds an approval to one call.

    :param str tool: the tool's name
    :param dict arguments: the validated arguments
    :rtype: str
    """
    raw = json.dumps(
        {"tool": tool, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()
