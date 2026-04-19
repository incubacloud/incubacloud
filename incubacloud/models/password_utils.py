"""
Fernet-based symmetric encryption for sensitive fields (passwords, passphrases).

Key resolution:
  The environment variable INCUBACLOUD_SECRET_KEY MUST be set in all environments
  (production, staging, local dev, test).  There is no fallback key.

  Rationale: a fallback dev key is useless when restoring a production database
  locally — the data was encrypted with the production key, not the dev key —
  and actively dangerous because it masks a misconfiguration.

  Generate a key with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

The prefix ``enc:`` distinguishes already-encrypted values from plain-text
values that may exist in older records before this feature was introduced.

Failure mode (fail-loud):
  ``encrypt_value`` and ``decrypt_value`` now raise when the key is missing
  or invalid, instead of silently writing plain text to the database.  This
  prevents the worst failure mode: an operator forgetting to configure the
  key in production and the system quietly persisting every SSH password,
  S3 secret and passphrase as plain text until someone notices.
"""
import logging
import os
import secrets

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "enc:"
_KEY_ENV_VAR = "INCUBACLOUD_SECRET_KEY"

# Lazy-loaded Fernet instance
_fernet = None


class IncubacloudCryptoError(RuntimeError):
    """Raised when encryption/decryption cannot proceed safely."""


def _key_error_message():
    return (
        f"{_KEY_ENV_VAR} environment variable is not set or invalid. "
        "Generate a key with: "
        'python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())" '
        f"and expose it to the Odoo container as {_KEY_ENV_VAR}."
    )


def _get_fernet(env=None):
    """Return a Fernet instance, initialising the key once per process.

    Returns None when the key cannot be resolved. Callers are expected to
    raise ``IncubacloudCryptoError`` (or propagate) rather than fall back
    to plain text.
    """
    global _fernet
    if _fernet is not None:
        return _fernet

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        _logger.error(
            "cryptography library not installed. "
            "Run: pip install cryptography"
        )
        return None

    raw_key = os.environ.get(_KEY_ENV_VAR, "").strip()

    if not raw_key:
        _logger.error(
            "%s environment variable is not set.", _KEY_ENV_VAR,
        )
        return None

    try:
        key_bytes = raw_key.encode() if isinstance(raw_key, str) else raw_key
        _fernet = Fernet(key_bytes)
    except Exception as e:
        _logger.error("Invalid %s: %s", _KEY_ENV_VAR, e)
        _fernet = None

    return _fernet


def key_is_configured():
    """Return True if the secret key is present and valid.

    Safe to call from a post-init hook without triggering side effects.
    """
    return _get_fernet() is not None


def encrypt_value(value, env=None):
    """Encrypt *value* and return ``enc:<base64>``.

    Raises:
        IncubacloudCryptoError: when the secret key is missing or invalid.
        Exception: the underlying Fernet error is propagated unchanged if
            encryption itself fails for any reason.
    """
    if not value:
        return value
    if isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX):
        return value  # already encrypted
    f = _get_fernet(env)
    if f is None:
        raise IncubacloudCryptoError(_key_error_message())
    token = f.encrypt(value.encode()).decode()
    return f"{_ENCRYPTED_PREFIX}{token}"


def decrypt_value(value, env=None):
    """Decrypt a ``enc:<base64>`` value and return plain text.

    Legacy plain-text values (no ``enc:`` prefix) are returned unchanged —
    migration code relies on this pass-through behaviour.

    Raises:
        IncubacloudCryptoError: when the secret key is missing or invalid.
        ValueError: when decryption fails (key rotated or data corrupted).
    """
    if not value:
        return value
    if not (isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)):
        return value  # plain-text legacy value
    f = _get_fernet(env)
    if f is None:
        raise IncubacloudCryptoError(_key_error_message())
    try:
        token = value[len(_ENCRYPTED_PREFIX):].encode()
        return f.decrypt(token).decode()
    except Exception as e:
        raise ValueError(
            "Failed to decrypt stored secret; the encryption key may have "
            "been rotated or the ciphertext corrupted."
        ) from e


def is_encrypted(value):
    """Return True if *value* was encrypted by this module."""
    return isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)


def generate_password(length=20):
    """Return a cryptographically strong URL-safe password (~27 chars for length=20)."""
    return secrets.token_urlsafe(length)
