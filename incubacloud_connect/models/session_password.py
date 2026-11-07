# -*- coding: utf-8 -*-
"""The password a session opened by ``/ic/login`` holds on Odoo 8.0.

8.0 keeps the user's password in the session and checks it against the
database on every call (``OpenERPSession.check_security``, then
``res.users.check_credentials``); a session without one is logged out.
A magic link has no password to give it, so it gives one of its own: a
signed, expiring value bound to the database, the user and what the
user's row holds of their login and password, which ``res_users.py``
accepts in ``check_credentials``, the method Odoo 8 names for plugging
other ways to authenticate (``auth_oauth`` accepts its access token
there). Changing the user's password, login or archiving them ends such
sessions, as it ends the ones opened with the password.

The key lives in a file of Odoo's data directory, readable by Odoo
alone: on 8.0 every user may read ``ir.config_parameter``, so a key
stored there would let anyone sign a password for the administrator.

Written for Python 2.7 and 3, without Odoo, so the panel's tests read it.
"""
import binascii
import errno
import hashlib
import hmac
import os
import tempfile
import time

#: What every such password starts with: no other is looked at.
PREFIX = 'incubacloud-connect.'

#: How long one is accepted, in seconds: a week, as long as 8.0 keeps a
#: session nobody uses (``openerp.http.session_gc``).
LIFETIME = 7 * 24 * 3600


def _bytes(value):
    """Return *value* as UTF-8 bytes.

    :param value: text or bytes.
    :rtype: bytes
    """
    if isinstance(value, bytes):
        return value
    return value.encode('utf-8')


def _mac(key, db, uid, fingerprint, expires, nonce):
    """Return the signature of one password, as hexadecimal bytes.

    :param bytes key: the key of ``load_key``.
    :param str db: the database the session belongs to.
    :param int uid: the user it is logged in as.
    :param fingerprint: what the user's row holds of their login and
        password (``res_users._fingerprint``).
    :param int expires: when it stops being accepted, a Unix time.
    :param str nonce: its random part.
    :rtype: bytes
    """
    message = b'\x00'.join(
        _bytes(part) for part in (
            db, '%d' % uid, fingerprint, '%d' % expires, nonce,
        )
    )
    return _bytes(hmac.new(key, message, hashlib.sha256).hexdigest())


def make(key, db, uid, fingerprint, now=None):
    """Return a password for a session of *uid* on *db*.

    :param bytes key: the key of ``load_key``.
    :param str db: the database.
    :param int uid: the user.
    :param fingerprint: see ``_mac``.
    :param now: the current Unix time; ``time.time()`` when None.
    :rtype: str
    """
    expires = int((time.time() if now is None else now) + LIFETIME)
    nonce = binascii.hexlify(os.urandom(16)).decode('ascii')
    mac = _mac(key, db, uid, fingerprint, expires, nonce).decode('ascii')
    return '%s%d.%s.%s' % (PREFIX, expires, nonce, mac)


def valid(key, db, uid, fingerprint, password, now=None):
    """Whether *password* is one ``make`` gave for *uid* on *db*, still in time.

    :param bytes key: the key of ``load_key``.
    :param str db: the database.
    :param int uid: the user being checked.
    :param fingerprint: see ``_mac``; the row as it is now.
    :param password: what the session, or a caller, presents.
    :param now: the current Unix time; ``time.time()`` when None.
    :rtype: bool
    """
    if not password or not password.startswith(PREFIX):
        return False
    try:
        expires, nonce, mac = password[len(PREFIX):].split('.')
        expires = int(expires)
    except ValueError:
        return False
    if expires < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(
        _bytes(mac), _mac(key, db, uid, fingerprint, expires, nonce),
    )


def load_key(path):
    """Return the key in the file at *path*, creating it the first time.

    Created whole or not at all: written to a file of its own, owner-only,
    and linked into place, so two workers creating it at once both read
    the one that won.

    :param str path: the key's file, in Odoo's data directory.
    :raises ValueError: when the file exists and is empty.
    :rtype: bytes
    """
    try:
        with open(path, 'rb') as handle:  # noqa: FURB101 — Python 2 has no pathlib
            key = handle.read()
    except (IOError, OSError) as exc:
        if exc.errno != errno.ENOENT:
            raise
        fd, draft = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(binascii.hexlify(os.urandom(32)))
            try:
                os.link(draft, path)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
        finally:
            os.unlink(draft)  # noqa: FURB144 — Python 2 has no pathlib
        with open(path, 'rb') as handle:  # noqa: FURB101 — Python 2 has no pathlib
            key = handle.read()
    if not key:
        raise ValueError('empty key file: %s' % path)
    return key
