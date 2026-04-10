"""
Fernet-based symmetric encryption for sensitive fields (passwords, passphrases).

Key resolution:
  1. Environment variable  INCUBACLOUD_SECRET_KEY  (Fernet-compatible base64url key)
  2. Fixed development key (only when env var is absent)

In production / staging, the env var MUST be set — otherwise encryption
falls back to the well-known dev key which offers no real protection.

The prefix ``enc:`` distinguishes already-encrypted values from plain-text
values that may exist in older records before this feature was introduced.
"""
import base64
import logging
import os
import secrets

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "enc:"

# Well-known key used ONLY in development when INCUBACLOUD_SECRET_KEY is unset.
# Provides functional encryption so EncryptedChar works locally, but offers
# zero security — anyone who reads this source can decrypt the values.
_DEV_KEY = "ZGV2ZWxvcG1lbnQta2V5LWRvLW5vdC11c2U="  # base64(b"development-key-do-not-use")
# Fernet needs a 32-byte url-safe base64 key.  Derive one deterministically:
_DEV_FERNET_KEY = base64.urlsafe_b64encode(
    _DEV_KEY.encode().ljust(32, b"\0")[:32]
).decode()

# Lazy-loaded Fernet instance
_fernet = None


def _get_fernet(env=None):
    """Return a Fernet instance, initialising the key once per process."""
    global _fernet
    if _fernet is not None:
        return _fernet

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        _logger.error(
            "cryptography library not installed. "
            "Password encryption is disabled. "
            "Run: pip install cryptography"
        )
        return None

    raw_key = os.environ.get("INCUBACLOUD_SECRET_KEY", "").strip()

    if not raw_key:
        running_env = os.environ.get("RUNNING_ENV", "").lower()
        if running_env in ("production", "staging"):
            _logger.error(
                "INCUBACLOUD_SECRET_KEY environment variable is required "
                "in %s.  Set it to a Fernet-compatible base64url key.",
                running_env,
            )
            return None
        raw_key = _DEV_FERNET_KEY
        _logger.warning(
            "INCUBACLOUD_SECRET_KEY not set — using development key. "
            "Do NOT use this in production."
        )

    try:
        key_bytes = raw_key.encode() if isinstance(raw_key, str) else raw_key
        _fernet = Fernet(key_bytes)
    except Exception as e:
        _logger.error("Invalid INCUBACLOUD_SECRET_KEY: %s", e)
        _fernet = None

    return _fernet


def encrypt_value(value, env=None):
    """Encrypt *value* and return ``enc:<base64>``.  Returns plain *value* on error."""
    if not value:
        return value
    if isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX):
        return value  # already encrypted
    f = _get_fernet(env)
    if f is None:
        return value
    try:
        token = f.encrypt(value.encode()).decode()
        return f"{_ENCRYPTED_PREFIX}{token}"
    except Exception as e:
        _logger.error("Failed to encrypt value: %s", e)
        return value


def decrypt_value(value, env=None):
    """Decrypt a ``enc:<base64>`` value and return plain text.  Returns *value* unchanged if not encrypted."""
    if not value:
        return value
    if not (isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)):
        return value  # plain-text legacy value
    f = _get_fernet(env)
    if f is None:
        return value
    try:
        token = value[len(_ENCRYPTED_PREFIX):].encode()
        return f.decrypt(token).decode()
    except Exception as e:
        _logger.error("Failed to decrypt value: %s", e)
        return value


def is_encrypted(value):
    """Return True if *value* was encrypted by this module."""
    return isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)


def generate_password(length=20):
    """Return a cryptographically strong URL-safe password (~27 chars for length=20)."""
    return secrets.token_urlsafe(length)
