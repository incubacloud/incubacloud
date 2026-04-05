import json
import logging
import time
from contextlib import suppress
from pathlib import Path

import odoo
import odoo.api
import werkzeug.utils
from odoo import http
from odoo.modules.registry import Registry
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

_TOKEN_DIR = '/tmp/ic_tokens'
_TOKEN_TTL = 60  # seconds
_MAJOR = int(odoo.release.version_info[0])


def _redirect(url):
    """Version-safe redirect (request.redirect exists only in v17+)."""
    if hasattr(request, 'redirect'):
        return request.redirect(url)
    return werkzeug.utils.redirect(url, code=303)


class IncubaCloudConnectController(http.Controller):

    @http.route(
        '/ic/login',
        type='http',
        auth='none',
        csrf=False,
        methods=['GET'],
    )
    def ic_login(self, t=None, **_kw):
        """Magic-link login — compatible with Odoo v7.0 through v19.0.

        Validates the one-time token written by the IncubaCloud panel,
        then uses Odoo's own session API to authenticate the user and
        redirects to /web.

        Security properties:
        - Token is 32 hex chars (128-bit random) — unforgeable
        - Single use: token file deleted before doing anything
        - 60-second TTL: expired tokens are rejected
        - Token files are 0o600 in a 0o700 directory
        - Session token is computed by Odoo itself (v11+; skipped for v7-v10)
        """
        if not t or len(t) != 32 or not all(
            c in '0123456789abcdef' for c in t
        ):
            _logger.warning(
                '/ic/login: malformed token from %s',
                request.httprequest.remote_addr,
            )
            return Response('Invalid request.', status=400)

        token_path = Path(_TOKEN_DIR) / t

        try:
            data = json.loads(token_path.read_text())
            token_path.unlink()
        except FileNotFoundError:
            return Response('Invalid or expired token.', status=400)
        except Exception:
            _logger.warning('/ic/login: error reading token %s', token_path)
            with suppress(Exception):
                token_path.unlink()
            return Response('Invalid or expired token.', status=400)

        if time.time() - data.get('ts', 0) > _TOKEN_TTL:
            return Response('Token expired.', status=400)

        uid = data.get('uid')
        db = data.get('db') or request.db
        if not isinstance(uid, int) or uid <= 0 or not db:
            return Response('Invalid token data.', status=400)

        # Verify user and compute session token using Odoo's own method.
        # _compute_session_token exists since v11; v7-v10 skip it.
        try:
            registry = Registry(db)
            with registry.cursor() as cr:
                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                user = env['res.users'].browse(uid)
                if not user.exists() or not user.active:
                    return Response('User not found.', status=400)
                login = user.login
                session_token = None
                if hasattr(user, '_compute_session_token'):
                    session_token = user._compute_session_token(
                        request.session.sid
                    )
        except Exception:
            _logger.exception(
                '/ic/login: error setting up session for uid=%s db=%s',
                uid, db,
            )
            return Response('Internal error.', status=500)

        # Populate session — Odoo saves it automatically at response end
        request.session['uid'] = uid
        request.session['login'] = login
        request.session['db'] = db
        if session_token is not None:
            request.session['session_token'] = session_token

        # Redirect admins to /odoo (v18+), everyone else to /web
        if _MAJOR >= 18:
            with suppress(Exception):
                registry = Registry(db)
                with registry.cursor() as cr:
                    env = odoo.api.Environment(cr, uid, {})
                    if env['res.users'].browse(uid).has_group(
                        'base.group_system',
                    ):
                        return _redirect('/odoo')
        return _redirect('/web')
