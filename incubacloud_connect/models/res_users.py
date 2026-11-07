# -*- coding: utf-8 -*-
"""Odoo 8.0 only: accept the password of a session ``/ic/login`` opened.

See ``session_password``. Loaded by ``models/__init__`` on 8.0 alone; it
uses 8.0's API, Python 2.
"""
import os

import openerp
from openerp.osv import osv
from openerp.tools import config

from . import session_password

#: The key's file name in Odoo's data directory.
_KEY_FILE = 'incubacloud_connect.key'


def _key():
    """Return this instance's signing key (``session_password.load_key``).

    :rtype: bytes
    """
    return session_password.load_key(os.path.join(config['data_dir'], _KEY_FILE))


def _fingerprint(cr, uid):
    """Return what the row of *uid* holds of its login and password.

    Its login and its password, in clear (``password``) or hashed by
    ``auth_crypt`` (``password_crypt``), whichever the database has.

    :param cr: a cursor of the database.
    :param int uid: the user.
    :return: the fingerprint, or None for a user that does not exist or
        is archived.
    :rtype: unicode
    """
    cr.execute('SELECT * FROM res_users WHERE id = %s', (uid,))
    row = cr.dictfetchone()
    if not row or not row.get('active'):
        return None
    return u'\x00'.join(
        row.get(column) or u'' for column in ('login', 'password', 'password_crypt')
    )


def password_for(cr, uid):
    """Return the password a session of *uid* opened by ``/ic/login`` holds.

    :param cr: a cursor of the database.
    :param int uid: an active user.
    :rtype: str
    """
    return session_password.make(_key(), cr.dbname, uid, _fingerprint(cr, uid))


def _session():
    """Return the user and the password of the HTTP request's session.

    :return: ``(uid, password)``, or ``(None, None)`` outside an HTTP
        request (XML-RPC, a cron), where ``openerp.http.request`` is
        bound to nothing.
    :rtype: tuple
    """
    try:
        session = openerp.http.request.session
    except RuntimeError:
        return None, None
    return session.uid, session.password


class res_users(osv.osv):
    _inherit = 'res.users'

    def check_credentials(self, cr, uid, password):
        """Accept the password of ``password_for`` besides the user's own.

        Only as the password of the request's own session
        (``session_password.accepts``); a failure to check it is logged
        and refused as a wrong password.

        :raises openerp.exceptions.AccessDenied: when it is neither.
        """
        try:
            return super(res_users, self).check_credentials(cr, uid, password)
        except openerp.exceptions.AccessDenied:
            session_uid, session_secret = _session()
            if not session_password.accepts(
                password, cr.dbname, uid, session_uid, session_secret, _key,
                lambda: _fingerprint(cr, uid),
            ):
                raise

    def check(self, db, uid, passwd):
        """Check a session password on every call, never from the cache.

        See ``session_password.check``: Odoo's own check, for any other
        password. The cursor is of this model's registry, as Odoo's own
        check takes it: 8.0 reads the database from ``self.pool``, not from
        what the caller names.

        :raises openerp.exceptions.AccessDenied: when it is refused.
        """
        session_password.check(
            passwd,
            self.pool.cursor,
            lambda cr: self.check_credentials(cr, uid, passwd),
            lambda: super(res_users, self).check(db, uid, passwd),
        )

    def _login(self, db, login, password):
        """Refuse a session password but for the session that holds it.

        It opens no session of its own: from XML-RPC, or from a browser
        whose session is another, it is refused before the user is even
        looked up. Its own session still logs in with it: 8.0's web
        client checks its session that way on every load
        (``/web/session/get_session_info``, ``OpenERPSession.ensure_valid``
        calls ``common.login``), and ``check_credentials`` then holds it to
        that session's user.

        :return: the user's id, or False when the login is refused.
        """
        if session_password.looks_like(password) and _session()[1] != password:
            return False
        return super(res_users, self)._login(db, login, password)

    def change_password(self, cr, uid, old_passwd, new_passwd, context=None):
        """Refuse a session password as the old one of a change.

        :raises openerp.exceptions.AccessDenied: when *old_passwd* is one.
        """
        if session_password.looks_like(old_passwd):
            raise openerp.exceptions.AccessDenied()
        return super(res_users, self).change_password(
            cr, uid, old_passwd, new_passwd, context=context,
        )
