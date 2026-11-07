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


class res_users(osv.osv):
    _inherit = 'res.users'

    def check_credentials(self, cr, uid, password):
        """Accept the password of ``password_for`` besides the user's own.

        :raises openerp.exceptions.AccessDenied: when it is neither.
        """
        try:
            return super(res_users, self).check_credentials(cr, uid, password)
        except openerp.exceptions.AccessDenied:
            fingerprint = _fingerprint(cr, uid)
            if fingerprint is None or not session_password.valid(
                _key(), cr.dbname, uid, fingerprint, password,
            ):
                raise
