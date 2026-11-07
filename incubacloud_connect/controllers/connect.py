# -*- coding: utf-8 -*-
# Python 2 (9.0 and 10.0) as well as 3, and 9.0's ``openerp`` package: the
# module is installed on every series a panel deploys, so it keeps to what
# all of their interpreters and APIs have.
import errno
import importlib
import io
import json
import logging
import os
import time

import werkzeug.utils

# ``openerp`` first: on 9.0, ``import odoo`` finds the source tree's
# ``odoo.py`` launcher instead. From 10.0 on there is no ``openerp``.
try:
    import openerp as odoo
except ImportError:
    import odoo

http = importlib.import_module(odoo.__name__ + '.http')
odoo_api = importlib.import_module(odoo.__name__ + '.api')
odoo_registry = importlib.import_module(odoo.__name__ + '.modules.registry')
request = http.request
Response = http.Response

_logger = logging.getLogger(__name__)

_TOKEN_DIR = '/tmp/ic_tokens'
_TOKEN_TTL = 60  # seconds
_MAJOR = int(odoo.release.version_info[0])


class _suppress(object):
    """``contextlib.suppress``, which Python 2 lacks: swallow *exceptions*.

    :param exceptions: the exception classes to swallow.
    """

    def __init__(self, *exceptions):
        self._exceptions = exceptions

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, self._exceptions)


def _registry(db):
    """Return the registry of *db*, the way the series names it.

    ``Registry(db)`` from 10.0 on; ``RegistryManager.get(db)`` before.

    :param str db: the database name.
    """
    if _MAJOR >= 10:
        return odoo_registry.Registry(db)
    return odoo_registry.RegistryManager.get(db)


def _take_token(path):
    """Read the token file at *path* and remove it, whatever it holds.

    :param str path: the token file.
    :returns: its JSON content, or None when there is no such file.
    :raises Exception: when it exists but cannot be read or parsed; the
        file is removed first if that is still possible.
    """
    try:
        with io.open(path, encoding='utf-8') as token_file:
            content = token_file.read()
    except (IOError, OSError) as exc:
        if exc.errno == errno.ENOENT:
            return None
        with _suppress(Exception):
            os.unlink(path)  # noqa: FURB144 — Python 2 has no pathlib
        raise
    try:
        os.unlink(path)  # noqa: FURB144 — Python 2 has no pathlib
    except (IOError, OSError) as exc:
        if exc.errno == errno.ENOENT:
            # Taken by a concurrent request in between: single use.
            return None
        raise
    return json.loads(content)


def _redirect(url):
    """Version-safe redirect (request.redirect exists only in v17+)."""
    if hasattr(request, 'redirect'):
        return request.redirect(url)
    return werkzeug.utils.redirect(url, code=303)


def _renew_sid(session):
    """Give *session* a new sid before it is logged in, as Odoo's login does.

    The sid a browser brings to ``/ic/login`` may belong to a session of
    another user. Logging in on it keeps a sid the browser, or whoever
    set its cookie, chose (session fixation), and the session token
    computed for it comes from ``_compute_session_token``'s cache, keyed
    on the sid alone: a worker that checked the other user's session
    returns that user's token, the other workers compute the right one
    and log the session out.

    Odoo 16 and later rotate a session flagged ``should_rotate`` when
    they save it at the end of the request: the stored session is
    deleted, a new sid drawn and the session token computed for it.
    Odoo 8 to 15 rotate a session flagged ``rotate`` the same way, but
    only 12 to 15 compute the token again, so the sid is renewed here
    as their ``get_response`` does it, and the caller computes the token
    for the new sid.

    :param session: the request's session, not yet logged in as the
        user of the link.
    :returns: whether Odoo computes the session token itself, at save
        time (16 and later).
    :rtype: bool
    """
    if _MAJOR >= 16:
        session.should_rotate = True
        return True
    store = http.root.session_store
    store.delete(session)
    session.sid = store.generate_key()
    session.modified = True
    return False


def _user_context(env, uid, session):
    """Return the session context Odoo's own login gives *uid*.

    The session the link logs in may carry the context of whoever used
    it before (their language, their timezone, their ``uid``): Odoo's
    login replaces it, and so does this, the way each version does.

    - 16 and later (``Session.finalize``; ``odoo.http.session.finalize``
      in 20): ``dict(res.users.context_get())`` as the user, which since
      17 carries ``uid`` itself.
    - 8 to 15 (``OpenERPSession.get_context``): the same with ``uid``
      added and the language passed through the session's
      ``_fix_lang``. 8 and 9 call ``context_get`` the old way, as their
      ``get_context`` does.

    :param env: an environment of the link's database as *uid*.
    :param int uid: the user the link logs in.
    :param session: the request's session, for ``_fix_lang`` (8 to 15).
    :returns: the context to store in the session.
    :rtype: dict
    """
    if _MAJOR < 10:
        context = dict(env.registry['res.users'].context_get(env.cr, uid) or {})
    else:
        context = dict(env['res.users'].context_get() or {})
    if _MAJOR < 16:
        context['uid'] = uid
        fix_lang = getattr(session, '_fix_lang', None)
        if fix_lang:
            fix_lang(context)
    return context


class IncubaCloudConnectController(http.Controller):

    @http.route(
        '/ic/login',
        type='http',
        auth='none',
        csrf=False,
        methods=['GET'],
    )
    def ic_login(self, t=None, **_kw):
        """Magic-link login — Odoo 9.0 through 20.0 (see ``controllers/__init__``).

        Validates the one-time token written by the IncubaCloud panel,
        then uses Odoo's own session API to authenticate the user and
        redirects to /web.

        Security properties:
        - Token is 32 hex chars (128-bit random) — unforgeable
        - Single use: token file deleted before doing anything
        - 60-second TTL: expired tokens are rejected
        - Token files are 0o600 in a 0o700 directory
        - Every login gets a new session id (``_renew_sid``): none the
          browser brought, logged in or not, is reused
        - Session token is computed by Odoo itself for that new id
          (v9+; v7-v8 have none)
        - The session's context is the user's own (``_user_context``,
          v8+): nothing of the session's previous user is kept
        """
        if not t or len(t) != 32 or not all(
            c in '0123456789abcdef' for c in t
        ):
            _logger.warning(
                '/ic/login: malformed token from %s',
                request.httprequest.remote_addr,
            )
            return Response('Invalid request.', status=400)

        token_path = os.path.join(_TOKEN_DIR, t)

        try:
            data = _take_token(token_path)
        except Exception:
            # Removed already by ``_take_token``, if that could be done.
            _logger.warning('/ic/login: error reading token %s', token_path)
            return Response('Invalid or expired token.', status=400)
        if data is None:
            return Response('Invalid or expired token.', status=400)

        if time.time() - data.get('ts', 0) > _TOKEN_TTL:
            return Response('Token expired.', status=400)

        uid = data.get('uid')
        db = data.get('db') or request.db
        if not isinstance(uid, int) or uid <= 0 or not db:
            return Response('Invalid token data.', status=400)

        # Verify user, renew the sid and compute the session token for
        # the new sid using Odoo's own method (v16+: at save time).
        # _compute_session_token exists since v9; v7-v8 skip it.
        try:
            registry = _registry(db)
            with registry.cursor() as cr:
                env = odoo_api.Environment(cr, odoo.SUPERUSER_ID, {})
                user = env['res.users'].browse(uid)
                if not user.exists() or not user.active:
                    return Response('User not found.', status=400)
                login = user.login
                # The user's own language and timezone, not those of
                # whoever used the browser's session before (8+).
                context = (
                    _user_context(
                        odoo_api.Environment(cr, uid, {}), uid,
                        request.session,
                    )
                    if _MAJOR >= 8 else None
                )
                session_token = None
                if not _renew_sid(request.session) and hasattr(
                    user, '_compute_session_token',
                ):
                    session_token = user._compute_session_token(
                        request.session.sid
                    )
        except Exception:
            _logger.exception(
                '/ic/login: error setting up session for uid=%s db=%s',
                uid, db,
            )
            return Response('Internal error.', status=500)

        # Forensic marker: the tenant's own log is the only place where an
        # IncubaCloud-brokered session is distinguishable from a normal
        # login. One line, invisible to the end user, evidence for the
        # operator if the impersonation is ever questioned.
        _logger.info(
            'IncubaCloud connect-as: session opened for uid=%s (%s) '
            'by panel user %s',
            uid, login, data.get('by') or 'unknown',
        )

        # Populate session — Odoo saves it automatically at response end
        # (v16+ rotating it then and computing its session token).
        request.session['uid'] = uid
        request.session['login'] = login
        request.session['db'] = db
        if session_token is not None:
            request.session['session_token'] = session_token
        if context is not None:
            request.session['context'] = context

        # Redirect admins to /odoo (v18+), everyone else to /web
        if _MAJOR >= 18:
            with _suppress(Exception):
                registry = _registry(db)
                with registry.cursor() as cr:
                    env = odoo_api.Environment(cr, uid, {})
                    if env['res.users'].browse(uid).has_group(
                        'base.group_system',
                    ):
                        return _redirect('/odoo')
        return _redirect('/web')
