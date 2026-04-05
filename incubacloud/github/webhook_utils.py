"""Utilities for validating GitHub App webhook signatures.

GitHub signs each webhook delivery with HMAC-SHA256 using the webhook secret.
The signature is sent in the ``X-Hub-Signature-256`` request header as::

    sha256=<hex_digest>

Use ``hmac.compare_digest`` for the comparison to avoid timing-based attacks.
"""

import hashlib
import hmac


def validate_hmac_sha256(
    payload_bytes: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """Return True if the GitHub webhook signature is valid.

    Args:
        payload_bytes: Raw request body bytes.
        signature_header: Value of the ``X-Hub-Signature-256`` header.
        secret: The webhook secret configured in the GitHub App settings.

    Returns:
        ``True`` if the signature matches; ``False`` otherwise.
        Always returns ``False`` if ``secret`` is empty or None.
    """
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False

    secret_bytes = secret.encode() if isinstance(secret, str) else secret
    expected = "sha256=" + hmac.new(
        secret_bytes, payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
