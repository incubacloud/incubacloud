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

Both cookies are read from the raw ``Cookie`` header, by their exact
name (``_cookie_values``): werkzeug strips each name with ``str.strip()``,
which also drops Unicode spaces (U+00A0, U+2000, U+3000, U+0085...), so
`` __Host-session_bind`` with such a space in front, which a browser keeps
as a cookie of another name and lets any host of the domain set, would
read as this host's own binding.

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

#: What pads a cookie in a ``Cookie`` header: the ASCII space and tab
#: only (RFC 6265's ``OWS``), never the Unicode spaces ``str.strip()``
#: removes too.
_COOKIE_PADDING = ' \t'

#: The session key that says the session was already warned about a
#: session cookie set by another host, so the warning is logged once per
#: session rather than on every request.
_TOSSING_WARNED = 'incubacloud_tossed_session_cookie_warned'

#: Set once the wrappers are installed, so a second import (module reload,
#: test re-import) does not wrap the wrappers.
_PATCHED = False


def binding_of(sid):
    """Return the value of the binding cookie for session id *sid*.

    :param str sid: a session id, as the ``session_id`` cookie holds it.
    :rtype: str
    """
    return hashlib.sha256(sid.encode()).hexdigest()


def _cookie_values(httprequest, name):
    """Return the values of the cookies of *httprequest* named exactly *name*.

    Read from the raw ``Cookie`` header (as WSGI hands it over: the bytes
    the browser sent, decoded as latin-1), split on ``;``, with only the
    ASCII space and tab taken as padding. Not ``httprequest.cookies``:
    werkzeug strips names with ``str.strip()``, so a cookie whose name
    starts with a Unicode space would be read under *name*.

    :param httprequest: Odoo's wrapper of the incoming request.
    :param str name: the cookie name, compared exactly.
    :returns: the values, in the order the header lists them.
    :rtype: list[str]
    """
    values = []
    for part in httprequest.environ.get('HTTP_COOKIE', '').split(';'):
        key, sep, value = part.partition('=')
        if sep and key.strip(_COOKIE_PADDING) == name:
            values.append(value.strip(_COOKIE_PADDING))
    return values


def bound_session_id(httprequest):
    """Return the ``session_id`` Odoo may use for *httprequest*.

    Over HTTPS, the one value of the request's ``session_id`` cookies
    that its binding cookie names, in whatever order the browser lists
    them; ``None`` when there is no binding, more than one, or it names
    none of them. Both are read by their exact name (``_cookie_values``).
    Over plain HTTP, the first ``session_id``, as Odoo reads it.

    :param httprequest: Odoo's wrapper of the incoming request.
    :returns: the session id, or ``None`` for a new anonymous session.
    :rtype: str | None
    """
    if not httprequest.is_secure:
        sids = httprequest.cookies.getlist(SESSION_COOKIE)
        return sids[0] if sids else None
    bindings = _cookie_values(httprequest, BINDING_COOKIE)
    if len(bindings) != 1 or not bindings[0]:
        # Only this host sets the binding, with Path=/ and no Domain: a
        # browser holds one at most. Two are not a choice to make.
        return None
    for sid in _cookie_values(httprequest, SESSION_COOKIE):
        if hmac.compare_digest(binding_of(sid), bindings[0]):
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
        then delegates to *original*. Session cookies left unused beside
        another are logged as a WARNING once per session (marked in it),
        then at DEBUG.
    """

    @functools.wraps(original)
    def _get_session_and_dbname(self):
        httprequest = self.httprequest
        sid = bound_session_id(httprequest)
        tossed = 0
        if sid != httprequest._session_id__:
            # Every session cookie Odoo itself would have read, the ones
            # named with a Unicode space in front included.
            offered = len(httprequest.cookies.getlist(SESSION_COOKIE))
            tossed = offered - (sid is not None)
            httprequest._session_id__ = sid
            # Several session cookies only come from a parent domain; one
            # alone is also a browser that predates the binding.
            if offered <= 1:
                _logger.debug(
                    "%s: %d session cookie(s) without this host's binding not used",
                    httprequest.path, tossed,
                )
                tossed = 0
        session, dbname = original(self)
        if tossed:
            # Each request of the visitor carries the tossed cookie until
            # it expires: one warning per session, the rest at DEBUG.
            warned = session.get(_TOSSING_WARNED)
            _logger.log(
                logging.DEBUG if warned else logging.WARNING,
                "%s: %d session cookie(s) without this host's binding not used",
                httprequest.path, tossed,
            )
            if not warned:
                session[_TOSSING_WARNED] = True
        return session, dbname

    return _get_session_and_dbname


def _forward_set_cookie(self, *args, **kwargs):
    """Set a cookie through the ``set_cookie`` of the ``_Response`` that a
    ``Response`` facade wraps, as it is now.

    The facade's own ``set_cookie`` (a ``ProxyFunc``) holds the function
    ``_Response.set_cookie`` was when ``odoo.http`` was imported, so it
    would skip the wrapper installed on ``_Response`` since.

    :param self: the ``Response`` facade.
    :returns: ``None``, as the facade's own.
    """
    self._wrapped__.set_cookie(*args, **kwargs)


def _patch_set_cookie():
    """Install the wrappers on both of Odoo's cookie-emitting classes, and
    the reading of the bound session on ``Request``.

    ``_Response`` is the real ``werkzeug`` response (the public ``Response``
    is a facade that forwards to it), and ``FutureResponse`` is the header
    placeholder used while a request is still being dispatched. Both need
    wrapping: the session cookie is written through the second one on a
    normal request and through the first one when a session expires. The
    facade is made to forward to the wrapped ``_Response.set_cookie``: the
    redirection of an expired session (``HttpDispatcher.handle_error``) is
    a facade, whose ``set_cookie`` would otherwise skip the wrapper.

    Idempotent — importing this module twice does not stack wrappers.
    """
    global _PATCHED
    if _PATCHED:
        return
    http._Response.set_cookie = _harden(http._Response.set_cookie)
    http.FutureResponse.set_cookie = _harden(http.FutureResponse.set_cookie)
    http.Response.set_cookie = _forward_set_cookie
    http.Request._get_session_and_dbname = _bind_session_reading(
        http.Request._get_session_and_dbname,
    )
    _PATCHED = True
    _logger.debug("Hardened set_cookie on _Response, Response and "
                  "FutureResponse, and bound the session read on Request")


_patch_set_cookie()
