"""RS256 JWT generation for GitHub App authentication.

Uses only the `cryptography` library (already present in Odoo) — no PyJWT required.
The private key is consumed locally and is never logged.
"""

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _b64url(data: bytes | str | dict) -> str:
    """URL-safe base64 encode without padding."""
    if isinstance(data, dict):
        data = json.dumps(data, separators=(",", ":")).encode()
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_github_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Return a signed JWT for authenticating as the GitHub App.

    The JWT is valid for 10 minutes (GitHub's maximum). We back-date `iat`
    by 60 seconds to tolerate minor clock skew between servers.

    Args:
        app_id: GitHub App's numeric ID (as a string).
        private_key_pem: RSA private key in PEM format.
            This value is used only for signing and is never logged.

    Returns:
        Compact serialized JWT string (header.payload.signature).
    """
    now = int(time.time())
    header = _b64url({"alg": "RS256", "typ": "JWT"})
    payload = _b64url({
        "iat": now - 60,
        "exp": now + 600,
        "iss": app_id,
    })
    signing_input = f"{header}.{payload}".encode()

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem,
        password=None,
    )
    return f"{header}.{payload}.{_b64url(private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))}"
