"""
Harden the attributes Odoo puts on outgoing cookies.

Odoo emits its session cookie with ``HttpOnly`` and nothing else — no
``Secure``, no ``SameSite`` (see ``odoo/http.py``, ``Request._save_session``
and the ``SessionExpiredException`` handler). On an HTTPS deployment that
leaves two holes open:

  * A user who types the bare domain makes one cleartext request before the
    301 to HTTPS, and that request carries the session cookie. Anyone on the
    path gets a valid session. ``HttpOnly`` does not help here.
  * The cookie travels on cross-site requests. Odoo's CSRF tokens cover most
    of the fallout, but the cookie itself should not be leaving the site.

Rather than fork ``odoo.http`` we wrap the two ``set_cookie`` entry points.
Both wrappers only ever *tighten*: a caller that already asked for ``secure``
or a specific ``samesite`` is left alone.

``Secure`` is added only when the request being served is itself secure
(``request.httprequest.is_secure``, which reflects ``X-Forwarded-Proto`` when
``proxy_mode`` is on). A development instance served over plain HTTP is
therefore untouched and keeps working.

``SameSite=Lax`` is added only to the session cookie. Other cookies Odoo
sets — ``frontend_lang``, ``tz``, ``cids``, the livechat and website visitor
cookies — may legitimately need cross-site delivery, and tightening them is
not what this is for.

``Lax`` is safe for the OIDC flows this platform runs on: the authorization
endpoint is reached by a top-level GET navigation, and the panel and its
tenants share a registrable domain, so the browser treats them as same-site
anyway. Back-channel logout is server-to-server and carries no cookies.
"""

import functools
import logging

from odoo import http

_logger = logging.getLogger(__name__)

#: Cookies that get ``SameSite=Lax`` when the caller did not ask for a value.
#: Deliberately just the authentication cookie — see the module docstring.
SAMESITE_LAX_COOKIES = frozenset({'session_id'})

#: Set once the wrappers are installed, so a second import (module reload,
#: test re-import) does not wrap the wrappers.
_PATCHED = False


def _request_is_secure():
    """Return whether the request being served arrived over HTTPS.

    Defensive on purpose: cookies are also set from code paths that run
    without a request bound (crons, tests, ``werkzeug`` internals), and a
    missing request must degrade to "not secure" rather than raise.
    """
    try:
        request = http.request
        return bool(request and request.httprequest.is_secure)
    except Exception:  # pragma: no cover - no request bound
        return False


def _harden(original):
    """Wrap a ``set_cookie`` implementation so it tightens its own defaults.

    :param original: the unbound ``set_cookie`` being replaced.
    :returns: a wrapper with the same signature that fills in ``secure`` and
        ``samesite`` when the caller left them at their permissive default,
        then delegates to *original*.
    """

    @functools.wraps(original)
    def set_cookie(self, key, value='', max_age=None, expires=-1, path='/',
                   domain=None, secure=False, httponly=False, samesite=None,
                   cookie_type='required'):
        if not secure and _request_is_secure():
            secure = True
        if samesite is None and key in SAMESITE_LAX_COOKIES:
            samesite = 'Lax'
        return original(
            self, key, value=value, max_age=max_age, expires=expires,
            path=path, domain=domain, secure=secure, httponly=httponly,
            samesite=samesite, cookie_type=cookie_type,
        )

    return set_cookie


def _patch_set_cookie():
    """Install the wrappers on both of Odoo's cookie-emitting classes.

    ``_Response`` is the real ``werkzeug`` response (the public ``Response``
    is a facade that forwards to it), and ``FutureResponse`` is the header
    placeholder used while a request is still being dispatched. Both need
    wrapping: the session cookie is written through the second one on a
    normal request and through the first one when a session expires.

    Idempotent — importing this module twice does not stack wrappers.
    """
    global _PATCHED
    if _PATCHED:
        return
    http._Response.set_cookie = _harden(http._Response.set_cookie)
    http.FutureResponse.set_cookie = _harden(http.FutureResponse.set_cookie)
    _PATCHED = True
    _logger.debug("Hardened set_cookie on _Response and FutureResponse")


_patch_set_cookie()
