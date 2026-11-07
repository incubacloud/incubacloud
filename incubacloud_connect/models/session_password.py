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
Deleting that file rotates the key: the next worker to need it creates
another, and every session ``/ic/login`` opened before is logged out.

Such a password is accepted only as the password of the session it was
given to (``accepts``), never by Odoo's cache of checked passwords
(``check``), and it never opens a session of its own nor stands for the
old password of a change (``res_users.py``).

Written for Python 2.7 and 3, without Odoo, so the panel's tests read it.
"""
import binascii
import errno
import glob
import hashlib
import hmac
import logging
import os
import tempfile
import time

_logger = logging.getLogger(__name__)

try:
    #: What a password may be: ``str`` or ``unicode`` on Python 2.
    _TEXT = basestring  # noqa: F821 — Python 2 alone has it
except NameError:
    _TEXT = str

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


def looks_like(password):
    """Whether *password* is text that starts as ``make``'s passwords do.

    Nothing else is ever checked against the key, nor loads it.

    :param password: what the session, or a caller, presents.
    :rtype: bool
    """
    return isinstance(password, _TEXT) and password.startswith(PREFIX)


def accepts(password, db, uid, session_uid, session_password, key, fingerprint,
            now=None):
    """Whether ``res.users.check_credentials`` takes *password* for *uid*.

    Only a password of ``make``'s shape is looked at, and only as the
    password of the HTTP session it was given to: one presented by
    XML-RPC, by a cron or by any session other than its own is refused.
    Whatever fails on the way (the key's file, the database) refuses it
    too, and is logged: the caller then raises its own ``AccessDenied``.

    :param password: what the session, or a caller, presents.
    :param str db: the database.
    :param int uid: the user being checked.
    :param session_uid: the user of the request's session; None outside
        an HTTP request.
    :param session_password: the password of the request's session; None
        outside an HTTP request.
    :param key: a callable returning the key (``load_key``), called only
        for a password of the request's own session.
    :param fingerprint: a callable returning what the row of *uid* holds
        now (``res_users._fingerprint``), or None for a user that does not
        exist or is archived; called as *key* is.
    :param now: the current Unix time; ``time.time()`` when None.
    :rtype: bool
    """
    if not looks_like(password) or session_uid != uid:
        return False
    if not looks_like(session_password) or not hmac.compare_digest(
        _bytes(session_password), _bytes(password),
    ):
        return False
    try:
        current = fingerprint()
        return current is not None and valid(
            key(), db, uid, current, password, now=now,
        )
    except Exception:
        _logger.exception(
            'session password of uid=%s on %s: refused, it could not be checked',
            uid, db,
        )
        return False


def check(password, cursor, check_credentials, cached):
    """``res.users.check`` on 8.0, which keeps no session password.

    Odoo's own check remembers, in each worker, a password it accepted
    once and asks the database no more: a session password would then
    outlive its week and a change of the user's password in the workers
    that saw it before. Such a password is checked again on every call,
    with a cursor of its own; any other goes to Odoo's check.

    :param password: what the session, or a caller, presents.
    :param cursor: a callable returning a new cursor of the database,
        closed here.
    :param check_credentials: a callable taking that cursor and raising
        ``AccessDenied`` when the password is refused.
    :param cached: a callable running Odoo's own check, for any other
        password.
    :raises openerp.exceptions.AccessDenied: when the password is refused.
    """
    if not looks_like(password):
        cached()
        return
    cr = cursor()
    try:
        check_credentials(cr)
    finally:
        cr.close()


def _unlink(path):
    """Remove the file at *path*, if it is still there.

    :param str path: a file.
    """
    try:
        os.unlink(path)  # noqa: FURB144 — Python 2 has no pathlib
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise


def load_key(path):
    """Return the key in the file at *path*, creating it the first time.

    Created whole or not at all: written to a draft of its own, owner-only,
    synced to disk and linked into place, so two workers creating it at
    once both read the one that won, and a crash leaves no empty key.
    Once the key is in place, the drafts of an attempt that died before
    removing its own go too; a worker whose draft goes that way reads
    the key already linked.

    Deleting the file rotates the key: every password ``make`` gave
    before is refused, so every session ``/ic/login`` opened is logged
    out, and the next ``/ic/login`` creates another.

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
        folder = os.path.dirname(path)
        prefix = os.path.basename(path) + '.tmp'
        fd, draft = tempfile.mkstemp(dir=folder, prefix=prefix)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(binascii.hexlify(os.urandom(32)))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(draft, path)
            except OSError as exc:
                # EEXIST: another worker linked its key first. ENOENT: it
                # did, and removed this draft with the stale ones.
                if exc.errno not in (errno.EEXIST, errno.ENOENT):
                    raise
        finally:
            _unlink(draft)
        for stale in glob.glob(os.path.join(folder, prefix + '*')):
            _unlink(stale)
        with open(path, 'rb') as handle:  # noqa: FURB101 — Python 2 has no pathlib
            key = handle.read()
    if not key:
        raise ValueError('empty key file: %s' % path)
    return key
