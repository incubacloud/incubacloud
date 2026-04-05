"""In-memory cache for GitHub App installation tokens.

Tokens are stored in a module-level dictionary protected by a threading lock.
They are NEVER written to the database. The cache is process-local — a new
Odoo worker process starts with an empty cache and fills it on first use.

Expiry buffer: tokens are considered expired 60 seconds before their real
expiry time to avoid using a token that expires mid-request.
"""

import threading
import time

_EXPIRY_BUFFER_SECONDS = 60

_cache: dict[str, dict] = {}  # {installation_id: {token, expires_at}}
_lock = threading.Lock()


def get_token(installation_id: str) -> str | None:
    """Return a cached token if still valid, else None."""
    key = installation_id
    with _lock:
        entry = _cache.get(key)
        if entry and entry["expires_at"] - time.time() > _EXPIRY_BUFFER_SECONDS:
            return entry["token"]
    return None


def set_token(installation_id: str, token: str, expires_at: float) -> None:
    """Store a token with its expiry timestamp (Unix epoch seconds)."""
    key = installation_id
    with _lock:
        _cache[key] = {"token": token, "expires_at": expires_at}


def invalidate(installation_id: str) -> None:
    """Remove a cached token (e.g. after a 401 response)."""
    key = installation_id
    with _lock:
        _cache.pop(key, None)
