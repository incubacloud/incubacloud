"""
Fernet-based symmetric encryption for sensitive fields (passwords, passphrases).

Key resolution:
  The key chain MUST be set in all environments (production, staging,
  local dev, test), and is read from the first of these that is set:

    1. the environment variable INCUBACLOUD_SECRET_KEY;
    2. the file the environment variable INCUBACLOUD_SECRET_KEY_FILE
       names (a Docker secret, a systemd credential);
    3. the file ``secret_key_file`` names in the ``[incubacloud]``
       section of odoo.conf;
    4. ``secret_key`` in that same section (Odoo.sh, where the
       environment of the Odoo process cannot be set).

  A source that is set but empty, unreadable or malformed fails: the
  next one is never tried, so a misplaced key is noticed instead of a
  different one being used. The log names the source, never the key.

  There is no fallback key. A fallback dev key is useless when
  restoring a production database locally — the data was encrypted
  with the production key, not the dev key — and actively dangerous
  because it masks a misconfiguration.

  Generate a key with:
    python -c "from cryptography.fernet import Fernet; \\
               print(Fernet.generate_key().decode())"

Key rotation (MultiFernet):
  Every source accepts a *comma-separated* list of Fernet keys. The
  first key is the primary (used to encrypt new values); every key in
  the list can decrypt. That lets operators rotate keys without a
  flag-day: generate a new key, prepend it to the list, redeploy; old
  ciphertext keeps working because the old key is still in the list.
  Once every row has been re-encrypted (via rotate_value below) the
  retired key can be dropped from the chain.

  Examples (the same text in any of the four sources):
    INCUBACLOUD_SECRET_KEY="<primary>"                  # single key
    INCUBACLOUD_SECRET_KEY="<new_primary>,<old_key>"    # rotation

The prefix ``enc:`` distinguishes already-encrypted values from
plain-text values that may exist in older records before this feature
was introduced.

Failure mode (fail-loud):
  ``encrypt_value`` and ``decrypt_value`` raise when the key is
  missing or invalid, instead of silently writing plain text.
"""
import configparser
import logging
import os
import secrets
import stat
import time

from odoo.tools import config

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "enc:"
_KEY_ENV_VAR = "INCUBACLOUD_SECRET_KEY"
_KEY_FILE_ENV_VAR = "INCUBACLOUD_SECRET_KEY_FILE"
#: The odoo.conf section the key can be given in, and its two options.
_CONF_SECTION = "incubacloud"
_CONF_KEY_FILE = "secret_key_file"
_CONF_KEY = "secret_key"
#: The largest key file read. A chain of keys is a few hundred bytes; a
#: bigger file is not one, and is refused instead of read into memory.
_KEY_FILE_MAX_BYTES = 64 * 1024
#: How long, in seconds, a process waits before it looks for a missing or
#: invalid key again. Every encrypted field read or written asks for the
#: key: without a wait, a server with none would log an ERROR and read
#: the files from disk on each of them, thousands of times a minute. Long
#: enough to stop that, short enough that a key put in place while the
#: server runs (a file written afterwards) is found within the minute.
_KEY_RETRY_SECONDS = 30

# Lazy-loaded MultiFernet instance (single Fernet wrapped if only one key)
_fernet = None
# The last failure to load it: ``(fingerprint, retry_at)``, or None. The
# fingerprint is a hash of what decides where the key comes from (the two
# environment variables and the odoo.conf path), so a change there is
# tried at once; ``retry_at`` is a ``time.monotonic()`` reading.
_key_failure = None


class IncubacloudCryptoError(RuntimeError):
    """Raised when encryption/decryption cannot proceed safely."""


def _key_error_message():
    """Return the error raised when no usable key is configured.

    :return: what is wrong and where the key can be given
    :rtype: str
    """
    return (
        "The panel's encryption key is not set or invalid. It is read "
        f"from the first of: the {_KEY_ENV_VAR} environment variable; "
        f"the file the {_KEY_FILE_ENV_VAR} environment variable names; "
        f"the file {_CONF_KEY_FILE} names, or else {_CONF_KEY}, in the "
        f"[{_CONF_SECTION}] section of odoo.conf. Odoo's log says which "
        "one was read and what is wrong with it. Generate a key with: "
        'python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())". '
        "To rotate, set a comma-separated list with the new primary "
        "key first and the old key second."
    )


def _odoo_conf_path():
    """Return the path of the configuration file Odoo was started with.

    :return: the path, or ``None`` when Odoo names none
    :rtype: str | None
    """
    return config.get("config") or None


def _read_conf_section(path):
    """Return the ``[incubacloud]`` section of the odoo.conf at *path*.

    :param path: the odoo.conf to read, or ``None``
    :return: the section's options; empty when the file, or the section,
        does not exist
    :rtype: dict
    :raise IncubacloudCryptoError: when the file cannot be parsed. Only
        the kind of error is said: the parser's own message may quote a
        line of the file, and that line may hold the key.
    """
    if not path:
        return {}
    parser = configparser.RawConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as e:
        raise IncubacloudCryptoError(
            f"Cannot parse {path} ({type(e).__name__}), so the encryption "
            f"key in its [{_CONF_SECTION}] section cannot be read."
        ) from None
    if not parser.has_section(_CONF_SECTION):
        return {}
    return dict(parser.items(_CONF_SECTION))


def _read_key_file(path, source):
    """Return the content of the key file at *path*.

    The file must be a regular file (opening a FIFO would block the
    process, a device could be endless) of at most ``_KEY_FILE_MAX_BYTES``.
    One that every user of the server can read is read anyway, with a
    warning: it still holds the key.

    :param path: the file, as configured (``~`` is expanded); absolute
    :param source: how the file was named, for the errors
    :return: the file's text
    :rtype: str
    :raise IncubacloudCryptoError: when it cannot be read, is not a
        regular file, is too large or is not UTF-8 text
    """
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        raise IncubacloudCryptoError(
            f"Cannot read {source}: the path is not absolute. Give an "
            "absolute path: the working directory of an Odoo process "
            "is not one to rely on."
        )
    try:
        # Non-blocking, so a FIFO is opened to be refused, not waited on.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise IncubacloudCryptoError(
            f"Cannot read {source}: {e.strerror or type(e).__name__}."
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise IncubacloudCryptoError(
                f"Cannot read {source}: it is not a regular file."
            )
        if info.st_mode & stat.S_IROTH:
            _logger.warning(
                "The key file %s is readable by every user of the "
                "server (mode %04o). Restrict it to Odoo's user.",
                path, stat.S_IMODE(info.st_mode),
            )
        # Closes the descriptor, which is owned by the file object from here.
        with os.fdopen(fd, "rb") as handle:
            fd = None
            data = handle.read(_KEY_FILE_MAX_BYTES + 1)
    except OSError as e:
        raise IncubacloudCryptoError(
            f"Cannot read {source}: {e.strerror or type(e).__name__}."
        ) from None
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > _KEY_FILE_MAX_BYTES:
        raise IncubacloudCryptoError(
            f"Cannot read {source}: it is larger than "
            f"{_KEY_FILE_MAX_BYTES // 1024} KiB, too big to hold a key."
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise IncubacloudCryptoError(
            f"Cannot read {source}: not UTF-8 text."
        ) from None


def _read_secret_key():
    """Return the key chain from the first source that is set.

    The sources, in order: ``INCUBACLOUD_SECRET_KEY``; the file
    ``INCUBACLOUD_SECRET_KEY_FILE`` names; the file ``secret_key_file``
    names in odoo.conf's ``[incubacloud]`` section; ``secret_key`` in
    that section. A source counts as set even when empty, so a misplaced
    key fails rather than falls through to another one.

    :return: ``(source, chain)``: a description of the source, for the
        log and the errors, and its text, stripped; ``(None, "")`` when
        no source is set
    :rtype: tuple
    :raise IncubacloudCryptoError: when the source cannot be read
    """
    if _KEY_ENV_VAR in os.environ:
        return (
            f"the {_KEY_ENV_VAR} environment variable",
            os.environ[_KEY_ENV_VAR].strip(),
        )
    path = os.environ.get(_KEY_FILE_ENV_VAR)
    if path is not None:
        source = f"the file {path} named by {_KEY_FILE_ENV_VAR}"
        return source, _read_key_file(path, source).strip()
    conf = _odoo_conf_path()
    section = _read_conf_section(conf)
    if _CONF_KEY_FILE in section:
        path = section[_CONF_KEY_FILE]
        source = (
            f"the file {path} named by [{_CONF_SECTION}] {_CONF_KEY_FILE} "
            f"in {conf}"
        )
        return source, _read_key_file(path, source).strip()
    if _CONF_KEY in section:
        return (
            f"[{_CONF_SECTION}] {_CONF_KEY} in {conf}",
            section[_CONF_KEY].strip(),
        )
    return None, ""


def _parse_keys(raw):
    """Split the key chain on ',' and strip blanks. Returns a list of
    non-empty byte strings (Fernet accepts bytes)."""
    return [k.strip().encode() for k in raw.split(',') if k.strip()]


def _key_inputs():
    """Return a hash of what decides where the key is read from.

    Not the sources' content: the two environment variables and the
    odoo.conf path. In memory only, to tell a failure recorded for one
    configuration from the same call under another.

    :rtype: int
    """
    return hash((
        os.environ.get(_KEY_ENV_VAR),
        os.environ.get(_KEY_FILE_ENV_VAR),
        _odoo_conf_path(),
    ))


def _get_fernet(env=None):
    """Return a MultiFernet initialised from the key chain, or None.

    The chain comes from the first source set (``_read_secret_key``);
    which one, and how many keys it holds, is logged once, the key
    never.

    Single-key config returns a MultiFernet with one Fernet — encrypt
    and decrypt behave identically to a bare Fernet, so the rest of
    the module doesn't need to know whether rotation is in progress.

    When there is no usable key, the failure is logged and the sources
    are not read again for ``_KEY_RETRY_SECONDS``, unless what decides
    where the key comes from has changed: every encrypted field asks
    for it, and a server without one would otherwise log an ERROR and
    read the disk each time.
    """
    global _fernet, _key_failure
    if _fernet is not None:
        return _fernet
    fingerprint = _key_inputs()
    if _key_failure is not None:
        failed_for, retry_at = _key_failure
        if failed_for == fingerprint and time.monotonic() < retry_at:
            return None
    fernet = _load_fernet()
    if fernet is None:
        _key_failure = (fingerprint, time.monotonic() + _KEY_RETRY_SECONDS)
        return None
    _key_failure = None
    _fernet = fernet
    return fernet


def _load_fernet():
    """Build the MultiFernet from the key chain's source, logging why not.

    :return: the MultiFernet, or ``None`` when the key is missing or invalid
    """
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:
        _logger.error(
            "cryptography library not installed. "
            "Run: pip install cryptography"
        )
        return None

    try:
        source, raw_key = _read_secret_key()
    except IncubacloudCryptoError as e:
        _logger.error("%s", e)
        return None
    if source is None:
        _logger.error("%s", _key_error_message())
        return None
    if not raw_key:
        _logger.error("The encryption key in %s is empty.", source)
        return None

    keys = _parse_keys(raw_key)
    if not keys:
        _logger.error(
            "The encryption key in %s contains no usable keys.", source,
        )
        return None

    try:
        fernets = [Fernet(k) for k in keys]
    except Exception as e:  # noqa: BLE001 — Fernet's message holds no key
        _logger.error("Invalid encryption key in %s: %s", source, e)
        return None

    # MultiFernet.encrypt always uses fernets[0], so the operator
    # controls which key is primary via the order in the chain.
    fernet = MultiFernet(fernets)
    if len(fernets) > 1:
        _logger.info(
            "Encryption key read from %s: %d keys (rotation in "
            "progress); new values will be encrypted with the first key.",
            source, len(fernets),
        )
    else:
        _logger.info("Encryption key read from %s.", source)
    return fernet


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


def decrypt_value(value, env=None, ttl=None):
    """Decrypt a ``enc:<base64>`` value and return plain text.

    Legacy plain-text values (no ``enc:`` prefix) are returned unchanged —
    migration code relies on this pass-through behaviour.

    *ttl*, in seconds, refuses a value encrypted longer ago than that (the
    time Fernet stamps in every token); None accepts any age.

    Raises:
        IncubacloudCryptoError: when the secret key is missing or invalid.
        ValueError: when decryption fails (key rotated, data corrupted or,
            with *ttl*, the value too old).
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
        return f.decrypt(token, ttl=ttl).decode()
    except Exception as e:
        raise ValueError(
            "Failed to decrypt stored secret; the encryption key may have "
            "been rotated or the ciphertext corrupted."
        ) from e


def is_encrypted(value):
    """Return True if *value* was encrypted by this module."""
    return isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)


def rotate_value(value, env=None):
    """Re-encrypt *value* with the current primary key.

    Use this to migrate secrets to a freshly-rotated key without
    decrypt/encrypt dance. Pass-through for plain-text legacy values
    and empties. ``MultiFernet.rotate`` decrypts with any configured
    key and re-encrypts with the first (primary).

    Returns the new ``enc:<token>`` string.
    """
    if not value:
        return value
    if not (isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)):
        return value  # plain legacy value, nothing to rotate
    f = _get_fernet(env)
    if f is None:
        raise IncubacloudCryptoError(_key_error_message())
    try:
        old_token = value[len(_ENCRYPTED_PREFIX):].encode()
        new_token = f.rotate(old_token).decode()
    except Exception as e:
        raise ValueError(
            "Failed to rotate stored secret; key may be missing from "
            "the MultiFernet chain or the ciphertext is corrupted."
        ) from e
    return f"{_ENCRYPTED_PREFIX}{new_token}"


def is_on_primary_key(value, env=None):
    """Return whether *value* is already encrypted with the primary key.

    This is the question a key rotation actually has to answer, and
    until now nothing could: the rotation counts how many rows it
    *rewrote*, and it rewrites every row on every pass. Fernet puts a
    timestamp and a random IV in each token, so re-encrypting the same
    plaintext with the same key yields a different string every time
    and the "rotated" tally never reaches zero. An operator following
    RB-01 to the letter would wait forever for a number that cannot
    converge — or drop the old key on a hunch, which strands whatever
    had not moved yet, permanently.

    Asking the ciphertext which key opens it converges, because it is a
    property of the data rather than of the last write.

    :param value: a stored ``enc:`` string. Empty and plain-text legacy
        values return ``True``: there is nothing for a rotation to move,
        so counting them as pending would never let it finish either.
    :param env: optional Odoo environment, for the key lookup.
    :return: ``True`` when the primary key alone can decrypt it.
    :rtype: bool
    """
    if not value:
        return True
    if not (isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)):
        return True
    f = _get_fernet(env)
    if f is None:
        raise IncubacloudCryptoError(_key_error_message())
    # ``MultiFernet._fernets`` is the ordered chain; the first entry is
    # the primary, the one ``encrypt`` uses. Decrypting with just that
    # one is what separates "already moved" from "still on an old key".
    primary = f._fernets[0]
    token = value[len(_ENCRYPTED_PREFIX):].encode()
    try:
        primary.decrypt(token)
    except Exception:  # noqa: BLE001 — any failure means "not primary"
        return False
    return True


def generate_password(length=20):
    """Return a cryptographically strong URL-safe password (~27 chars for length=20)."""
    return secrets.token_urlsafe(length)


def generate_secret_key():
    """Return a new Fernet key, as the text ``INCUBACLOUD_SECRET_KEY`` holds.

    :return: one url-safe base64 key (44 characters)
    :rtype: str
    """
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def parse_secret_key_line(text):
    """Return the valid Fernet keys a key chain names, in order, once each.

    Accepts either the bare chain (``"k1,k2"``, as
    ``cloud.instance.secret_key`` stores it) or the content of an
    ``incubacloud.env`` file, whose ``INCUBACLOUD_SECRET_KEY=`` line is
    read and every other line ignored. Quotes around the value are
    dropped. A token that is not a Fernet key is left out rather than
    raised on: what is read from a host is not trusted to be well
    formed, and a chain must never carry a key Odoo would refuse.

    :param str text: a key chain, an env file's content, or ``None``
    :return: the keys, primary first
    :rtype: list[str]
    """
    from cryptography.fernet import Fernet

    if not text:
        return []
    raw = text.strip()
    prefix = f"{_KEY_ENV_VAR}="
    if prefix in raw or "\n" in raw:
        raw = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(prefix):
                raw = line[len(prefix):].strip()
    raw = raw.strip("'\"")
    keys = []
    for token in raw.split(","):
        token = token.strip()
        if not token or token in keys:
            continue
        try:
            Fernet(token.encode())
        except Exception:  # noqa: BLE001 — anything Fernet refuses is dropped
            continue
        keys.append(token)
    return keys


def merge_secret_key_lines(*chains):
    """Join key chains so that no key any of them holds is lost.

    The keys of the first chain come first — its first key stays the one
    new values are encrypted with — and every key of the following
    chains that is not there yet is appended. A value encrypted with any
    of them can still be read with the result: that is what makes it
    safe to merge what a host holds with what the panel keeps for it,
    whichever of the two is the stale one.

    :param chains: key chains or env file contents (see
        :func:`parse_secret_key_line`); empty ones are skipped
    :return: the merged chain (``"k1,k2"``), or ``""`` when none holds a
        valid key
    :rtype: str
    """
    merged = []
    for chain in chains:
        for key in parse_secret_key_line(chain):
            if key not in merged:
                merged.append(key)
    return ",".join(merged)
