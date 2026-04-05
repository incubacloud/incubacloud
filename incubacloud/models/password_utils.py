"""
Fernet-based symmetric encryption for sensitive fields (passwords, passphrases).

Key resolution order:
  1. Environment variable  INCUBACLOUD_SECRET_KEY  (base64url-encoded 32-byte key)
  2. Odoo system parameter  incubacloud.secret_key  (auto-generated on first use)

The prefix ``enc:`` distinguishes already-encrypted values from plain-text
values that may exist in older records before this feature was introduced.
"""
import base64
import logging
import os
import secrets

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "enc:"

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

    if not raw_key and env is not None:
        ICP = env["ir.config_parameter"].sudo()
        raw_key = ICP.get_param("incubacloud.secret_key", "")
        if not raw_key:
            running_env = os.environ.get("RUNNING_ENV", "").lower()
            if running_env in ("production", "staging"):
                _logger.error(
                    "INCUBACLOUD_SECRET_KEY environment variable is required "
                    "in %s. Set it to a Fernet-compatible base64url key. "
                    "Auto-generation is only allowed in development.",
                    running_env,
                )
                return None
            # Auto-generate and persist a new key (development only)
            raw_key = Fernet.generate_key().decode()
            ICP.set_param("incubacloud.secret_key", raw_key)
            _logger.info("Generated new incubacloud.secret_key in ir.config_parameter")

    if not raw_key:
        _logger.warning(
            "No INCUBACLOUD_SECRET_KEY env var and no env context available. "
            "Password encryption is disabled for this call."
        )
        return None

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
