"""
Harden the attributes Odoo puts on outgoing cookies, and which session
cookie it reads back.

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
endpoint is reached by a top-level GET navigation, and a panel and the
panels it serves share a registrable domain, so the browser treats them as same-site
anyway. Back-channel logout is server-to-server and carries no cookies.

Cookie tossing
==============
A browser sends a host every cookie of its parent domains, and any host
under the same registrable domain may set one there. A host serving code
somebody else wrote (an instance a panel deployed, on a name under the
panel's own domain) can therefore set ``session_id`` with
``Domain=<parent>``, and that cookie reaches the panel beside the panel's
own, or alone. The ``Cookie`` header does not say which is which, and
Odoo takes the first one it lists (``HTTPRequest.__init__``), which the
setter controls with a longer ``Path``. Two outcomes: the visitor is
served in the setter's account, typing what they type there (login CSRF),
or keeps being signed out. ``SameSite`` and ``Secure`` do not stop it (the
hosts are same-site, and the setter serves HTTPS too); Odoo rotates the
session at sign-in (``Session.finalize`` and ``Request._save_session``),
which stops session fixation, not this.

A cookie named with the ``__Host-`` prefix is only accepted from the host
itself, over HTTPS, with ``Path=/`` and no ``Domain``: no other host can
set it. Odoo's cookie name is fixed, so instead, whenever ``session_id``
is set on a secure request, ``__Host-session_bind`` is set beside it with
a digest of its value (``binding_of``), the same lifetime and
``SameSite``. On a secure request Odoo is given only the ``session_id``
that the binding names (``bound_session_id``); with none, it starts a new
anonymous session, as for a visitor without cookies. The digest is not a
secret: what protects it is that only this host can set the cookie.

Plain HTTP (a development instance) is left as Odoo has it: no
``__Host-`` cookie exists there. On the first secure request after this
is installed, a browser holding a session without its binding starts a new
one: everybody signs in once more.
"""

import functools
import hashlib
import hmac
import logging

from odoo import http

_logger = logging.getLogger(__name__)

#: Cookies that get ``SameSite=Lax`` when the caller did not ask for a value.
#: Deliberately just the authentication cookie — see the module docstring.
SAMESITE_LAX_COOKIES = frozenset({'session_id'})

#: Odoo's session cookie.
SESSION_COOKIE = 'session_id'

#: The cookie only this host can set, naming the session it set
#: (see "Cookie tossing" in the module docstring).
BINDING_COOKIE = '__Host-session_bind'

#: Set once the wrappers are installed, so a second import (module reload,
#: test re-import) does not wrap the wrappers.
_PATCHED = False


def binding_of(sid):
    """Return the value of the binding cookie for session id *sid*.

    :param str sid: a session id, as the ``session_id`` cookie holds it.
    :rtype: str
    """
    return hashlib.sha256(sid.encode()).hexdigest()


def bound_session_id(httprequest):
    """Return the ``session_id`` Odoo may use for *httprequest*.

    Over HTTPS, the one value of the request's ``session_id`` cookies
    that its binding cookie names, in whatever order the browser lists
    them; ``None`` when there is no binding or it names none of them.
    Over plain HTTP, the first ``session_id``, as Odoo reads it.

    :param httprequest: Odoo's wrapper of the incoming request.
    :returns: the session id, or ``None`` for a new anonymous session.
    :rtype: str | None
    """
    sids = httprequest.cookies.getlist(SESSION_COOKIE)
    if not httprequest.is_secure:
        return sids[0] if sids else None
    bound = httprequest.cookies.get(BINDING_COOKIE)
    if bound:
        for sid in sids:
            if hmac.compare_digest(binding_of(sid), bound):
                return sid
    return None


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
        result = original(
            self, key, value=value, max_age=max_age, expires=expires,
            path=path, domain=domain, secure=secure, httponly=httponly,
            samesite=samesite, cookie_type=cookie_type,
        )
        if key == SESSION_COOKIE and _request_is_secure():
            original(
                self, BINDING_COOKIE, value=binding_of(value) if value else '',
                max_age=max_age, expires=expires, path='/', domain=None,
                secure=True, httponly=True, samesite=samesite,
                cookie_type=cookie_type,
            )
        return result

    return set_cookie


def _bind_session_reading(original):
    """Wrap ``Request._get_session_and_dbname`` so Odoo loads the bound session.

    :param original: the unbound ``_get_session_and_dbname`` being replaced.
    :returns: a wrapper that replaces the session id Odoo read from the
        first ``session_id`` cookie with ``bound_session_id``'s answer,
        then delegates to *original*.
    """

    @functools.wraps(original)
    def _get_session_and_dbname(self):
        httprequest = self.httprequest
        sid = bound_session_id(httprequest)
        if sid != httprequest._session_id__:
            offered = len(httprequest.cookies.getlist(SESSION_COOKIE))
            # Several session cookies only come from a parent domain; one
            # alone is also a browser that predates the binding.
            _logger.log(
                logging.WARNING if offered > 1 else logging.DEBUG,
                "%s: %d session cookie(s) without this host's binding not used",
                httprequest.path, offered - (sid is not None),
            )
            httprequest._session_id__ = sid
        return original(self)

    return _get_session_and_dbname


def _patch_set_cookie():
    """Install the wrappers on both of Odoo's cookie-emitting classes, and
    the reading of the bound session on ``Request``.

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
    http.Request._get_session_and_dbname = _bind_session_reading(
        http.Request._get_session_and_dbname,
    )
    _PATCHED = True
    _logger.debug("Hardened set_cookie on _Response and FutureResponse, "
                  "and bound the session read on Request")


_patch_set_cookie()
