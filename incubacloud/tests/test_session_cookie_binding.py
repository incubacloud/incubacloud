"""A session cookie set from a sibling host is never the one Odoo uses.

A browser sends a panel every cookie of its parent domains, and any host
under the same registrable domain may set one there: a ``session_id``
with ``Domain=<parent>`` reaches the panel beside the panel's own, or
alone, and Odoo takes the first one the header lists (cookie tossing).
Over HTTPS the panel binds its session to a ``__Host-`` cookie, which no
other host can set, and uses only the ``session_id`` that cookie names.

The requests are real ones, against the test server, made secure the
way production makes them: ``proxy_mode`` on and the proxy's
``X-Forwarded-Proto: https``.
"""
import json
import logging
import os
import re
import time
from unittest.mock import patch

import requests
from werkzeug.test import EnvironBuilder

import odoo.http
from odoo.tests import new_test_user, tagged
from odoo.tests.common import BaseCase, HttpCase
from odoo.tools import config

from odoo.addons.bus.tests.common import WebsocketCase
from odoo.addons.bus.websocket import WebsocketConnectionHandler
from odoo.addons.incubacloud.models import http_session_cookie as cookie_module

from .test_real_ip import _OnTheUncorrectedProxyFix

#: The cookie that names the panel's own session.
BINDING = "__Host-session_bind"
CHROME = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
#: Spaces ``str.strip()`` removes and a browser keeps as part of a
#: cookie's name: `` __Host-session_bind`` is not a ``__Host-`` cookie,
#: so any host of the domain may set it.
UNICODE_SPACES = ("\u2000", "\u00a0", "\u3000", "\u0085")
#: Where the cookie module logs the session cookies it does not use.
LOGGER = "odoo.addons.incubacloud.models.http_session_cookie"


def _as_sent(cookie_header):
    """Return *cookie_header* as a browser sends it and WSGI hands it on.

    The browser sends the bytes in UTF-8, and the WSGI server decodes
    every header as latin-1.

    :param str cookie_header: the header, with any character.
    :rtype: str
    """
    return cookie_header.encode("utf-8").decode("latin-1")


def _spaces_as_sent(through_http_server=False):
    """Return the spellings of the spaces in front of a name a request
    can carry, as ``(label, characters)``.

    Each space in UTF-8, as WSGI hands it on (werkzeug 2's
    ``Request.cookies`` decodes it back, werkzeug 3's does not), and the
    two of them latin-1 has as one byte, which a host writing its own
    ``Set-Cookie`` header can put there and WSGI hands on as that very
    character, the one ``str.strip()`` removes.

    :param bool through_http_server: leave out the one byte 0x85, which
        Python's HTTP server takes as the end of a header line
        (``str.splitlines``) before Odoo sees it.
    :rtype: list[tuple[str, str]]
    """
    spellings = [(f"U+{ord(c):04X} in UTF-8", _as_sent(c)) for c in UNICODE_SPACES]
    spellings += [
        (f"U+{ord(c):04X} as one byte", c) for c in UNICODE_SPACES
        if ord(c) < 0x100 and not (through_http_server and c == "\u0085")
    ]
    return spellings


def _session_expired(cls):
    """Stand in for ``ir.http._auth_method_user`` on a session Odoo finds
    expired: raise what it raises then.

    :raises odoo.http.SessionExpiredException: always.
    """
    raise odoo.http.SessionExpiredException("Session expired")


def _request(cookie_header, scheme="https"):
    """Return Odoo's wrapper of a request carrying *cookie_header*.

    :param str cookie_header: the raw ``Cookie`` header, as a browser
        sends it (duplicate names included).
    :param str scheme: ``https`` or ``http``.
    :rtype: odoo.http.HTTPRequest
    """
    environ = EnvironBuilder(
        base_url=f"{scheme}://panel.example.com",
        headers={"Cookie": cookie_header},
    ).get_environ()
    return odoo.http.HTTPRequest(environ)


class TestBoundSessionId(BaseCase):
    """Which ``session_id`` of a request Odoo is given."""

    def test_the_bound_one_wins_in_either_order(self):
        bind = cookie_module.binding_of("own")
        for header in (
            f"session_id=tossed; session_id=own; {BINDING}={bind}",
            f"session_id=own; session_id=tossed; {BINDING}={bind}",
        ):
            with self.subTest(header=header):
                self.assertEqual(
                    cookie_module.bound_session_id(_request(header)), "own",
                )

    def test_a_session_without_its_binding_is_not_used(self):
        self.assertIsNone(
            cookie_module.bound_session_id(_request("session_id=tossed")),
        )
        other = cookie_module.binding_of("own")
        self.assertIsNone(cookie_module.bound_session_id(
            _request(f"session_id=tossed; {BINDING}={other}"),
        ))

    def test_a_binding_named_with_a_unicode_space_is_not_the_binding(self):
        """werkzeug strips the name to ``__Host-session_bind``; the browser
        took it from any host of the domain, under another name."""
        own, tossed = (cookie_module.binding_of(s) for s in ("own", "tossed"))
        for label, space in _spaces_as_sent():
            with self.subTest(space=label):
                beside = (
                    f"session_id=tossed; {space}{BINDING}={tossed}; "
                    f"session_id=own; {BINDING}={own}"
                )
                self.assertEqual(
                    cookie_module.bound_session_id(_request(beside)), "own",
                )
                alone = f"session_id=tossed; {space}{BINDING}={tossed}"
                self.assertIsNone(cookie_module.bound_session_id(_request(alone)))
                session = f"{space}session_id=own; {BINDING}={own}"
                self.assertIsNone(
                    cookie_module.bound_session_id(_request(session)),
                )

    def test_two_bindings_are_none(self):
        """This host sets one, with ``Path=/`` and no ``Domain``: a browser
        never holds two."""
        bind = cookie_module.binding_of("own")
        self.assertIsNone(cookie_module.bound_session_id(
            _request(f"session_id=own; {BINDING}={bind}; {BINDING}={bind}"),
        ))

    def test_no_session_cookie_is_no_session(self):
        bind = cookie_module.binding_of("own")
        self.assertIsNone(
            cookie_module.bound_session_id(_request(f"{BINDING}={bind}")),
        )

    def test_over_plain_http_the_first_one_is_kept(self):
        """No ``__Host-`` cookie exists without TLS: Odoo's choice stands."""
        self.assertEqual(
            cookie_module.bound_session_id(
                _request("session_id=first; session_id=second", "http"),
            ),
            "first",
        )


def _set_cookies(response):
    """Return the cookies a response sets: ``{name: (value, attributes)}``.

    :param requests.Response response: the test server's answer.
    :returns: per cookie, its value and its attributes, lower-cased
        names mapped to their value (``True`` for a flag).
    :rtype: dict
    """
    cookies = {}
    for line in response.raw.headers.getlist("Set-Cookie"):
        first, *rest = [part.strip() for part in line.split(";")]
        name, _, value = first.partition("=")
        attributes = {}
        for part in rest:
            key, sep, val = part.partition("=")
            attributes[key.lower()] = val if sep else True
        cookies[name] = (value, attributes)
    return cookies


@tagged("-at_install", "post_install")
class TestCookieTossing(_OnTheUncorrectedProxyFix, HttpCase):
    """The panel's session over HTTPS, with cookies tossed beside it."""

    def setUp(self):
        super().setUp()
        self.startPatcher(patch.dict(config.options, {"proxy_mode": True}))
        self.victim = new_test_user(
            self.env, login="tossed-victim", password="tossed-victim-pw",
            groups="base.group_user",
        )
        self.attacker = new_test_user(
            self.env, login="tossed-attacker", password="tossed-attacker-pw",
            groups="base.group_user",
        )

    def _headers(self, cookie=None, secure=True):
        """Return a browser's headers through the HTTPS proxy.

        :param str cookie: the raw ``Cookie`` header, if any.
        :param bool secure: whether the proxy says the visit was HTTPS.
        :rtype: dict
        """
        headers = {"User-Agent": CHROME}
        if secure:
            headers.update({
                "X-Forwarded-Host": self.base_url().split("://", 1)[1],
                "X-Forwarded-Proto": "https",
            })
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _open(self, path, cookie=None, secure=True, data=None, headers=None):
        """Send *path* to the test server with exactly the cookies given.

        A plain ``requests`` call, outside the test's opener: the opener
        would add its own cookie jar, and the test sends the ``Cookie``
        header a browser holding a tossed cookie would send. What the
        test wrote is flushed first and read afresh after.

        :param str path: the path to open, a POST when *data* is given.
        :param str cookie: the raw ``Cookie`` header, if any.
        :param bool secure: whether the proxy says the visit was HTTPS.
        :param data: the body, form fields or a JSON string.
        :param dict headers: headers to add.
        :rtype: requests.Response
        """
        self.env.flush_all()
        response = requests.request(
            "POST" if data is not None else "GET",
            self.base_url() + path,
            data=data,
            headers=self._headers(cookie, secure) | (headers or {}),
            allow_redirects=False,
            timeout=30,
        )
        self.env.invalidate_all()
        return response

    def _sign_in_with_the_form(self, login, password):
        """Sign in through ``/web/login`` as a browser does, over HTTPS.

        :returns: the anonymous session id the login page gave, the
            session id the login answered with, and that answer's cookies.
        :rtype: tuple
        """
        page = self._open("/web/login")
        self.assertEqual(page.status_code, 200)
        before = _set_cookies(page)
        anonymous = before["session_id"][0]
        # Stored, as it is once anything is written to it, so that what
        # the sign-in does to it can be seen.
        store = odoo.http.root.session_store
        store.save(store.get(anonymous))
        cookie = f"session_id={anonymous}; {BINDING}={before[BINDING][0]}"
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        answer = self._open(
            "/web/login",
            cookie=cookie,
            data={"login": login, "password": password, "csrf_token": csrf},
        )
        self.assertEqual(answer.status_code, 303, answer.text[:500])
        after = _set_cookies(answer)
        return anonymous, after["session_id"][0], after

    def _session_info(self, cookie, secure=True):
        """Ask ``/web/session/get_session_info`` with *cookie*.

        :rtype: requests.Response
        """
        return self._open(
            "/web/session/get_session_info",
            cookie=cookie,
            secure=secure,
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}),
            headers={"Content-Type": "application/json"},
        )

    def _uid(self, cookie, secure=True):
        """Return the user a request with *cookie* is served as.

        :returns: the user id, or ``None`` when the request has no
            signed-in session (Odoo answers ``SessionExpired``, code 100).
        """
        answer = self._session_info(cookie, secure).json()
        if "error" in answer:
            self.assertEqual(answer["error"]["code"], 100, answer)
            return None
        return answer["result"]["uid"]

    def test_signing_in_rotates_the_session_and_binds_the_new_one(self):
        """Odoo replaces the session at sign-in (``Session.finalize`` asks
        for the rotation, ``Request._save_session`` deletes the old one),
        and the new one comes with its binding, a cookie only this host
        holds."""
        anonymous, signed_in, cookies = self._sign_in_with_the_form(
            "tossed-victim", "tossed-victim-pw",
        )
        self.assertNotEqual(signed_in, anonymous)
        store = odoo.http.root.session_store
        self.assertFalse(os.path.isfile(store.get_session_filename(anonymous)))
        value, attributes = cookies[BINDING]
        self.assertEqual(value, cookie_module.binding_of(signed_in))
        self.assertTrue(attributes.get("secure"))
        self.assertTrue(attributes.get("httponly"))
        self.assertEqual(attributes.get("path"), "/")
        self.assertNotIn("domain", attributes)
        self.assertEqual(
            self._uid(f"session_id={signed_in}; {BINDING}={value}"),
            self.victim.id,
        )

    def test_a_session_tossed_beside_the_own_one_is_ignored(self):
        _anonymous, own, cookies = self._sign_in_with_the_form(
            "tossed-victim", "tossed-victim-pw",
        )
        bind = cookies[BINDING][0]
        tossed = self.authenticate("tossed-attacker", "tossed-attacker-pw").sid
        for cookie in (
            f"session_id={tossed}; session_id={own}; {BINDING}={bind}",
            f"session_id={own}; session_id={tossed}; {BINDING}={bind}",
        ):
            with self.subTest(cookie=cookie):
                self.assertEqual(self._uid(cookie), self.victim.id)

    def test_a_binding_named_with_a_unicode_space_is_ignored(self):
        """The name werkzeug strips to the binding's, sent the way a
        browser sends it: UTF-8, read as latin-1 by the WSGI server."""
        _anonymous, own, cookies = self._sign_in_with_the_form(
            "tossed-victim", "tossed-victim-pw",
        )
        bind = cookies[BINDING][0]
        tossed = self.authenticate("tossed-attacker", "tossed-attacker-pw").sid
        forged = cookie_module.binding_of(tossed)
        for label, space in _spaces_as_sent(through_http_server=True):
            with self.subTest(space=label):
                self.assertEqual(self._uid(
                    f"session_id={tossed}; {space}{BINDING}={forged}; "
                    f"session_id={own}; {BINDING}={bind}"
                ), self.victim.id)
                self.assertIsNone(self._uid(
                    f"session_id={tossed}; {space}{BINDING}={forged}"
                ))

    def test_a_tossed_cookie_is_warned_once_per_session(self):
        _anonymous, own, cookies = self._sign_in_with_the_form(
            "tossed-victim", "tossed-victim-pw",
        )
        tossed = self.authenticate("tossed-attacker", "tossed-attacker-pw").sid
        cookie = f"session_id={tossed}; session_id={own}; {BINDING}={cookies[BINDING][0]}"
        with self.assertLogs(LOGGER, logging.DEBUG) as logs:
            for _ in range(3):
                self.assertEqual(self._uid(cookie), self.victim.id)
        levels = [record.levelno for record in logs.records]
        self.assertEqual(levels.count(logging.WARNING), 1, logs.output)
        self.assertEqual(levels.count(logging.DEBUG), 2, logs.output)

    def test_the_expired_session_redirect_binds_the_new_session(self):
        """On an expired session a page answers with a redirection to the
        login, whose cookie Odoo sets on a ``_Response``
        (``HttpDispatcher.handle_error``), not through ``_save_session``."""
        _anonymous, own, cookies = self._sign_in_with_the_form(
            "tossed-victim", "tossed-victim-pw",
        )
        ir_http = type(self.env["ir.http"])
        with patch.object(ir_http, "_auth_method_user", classmethod(_session_expired)):
            answer = self._open(
                "/web/webclient/load_menus",
                cookie=f"session_id={own}; {BINDING}={cookies[BINDING][0]}",
            )
        self.assertIn(answer.status_code, (302, 303), answer.text[:500])
        sent = answer.raw.headers.getlist("Set-Cookie")
        self.assertIn("/web/login", answer.headers["Location"])
        after = _set_cookies(answer)
        new = after["session_id"][0]
        self.assertNotEqual(new, own)
        self.assertIn(BINDING, after, sent)
        value, attributes = after[BINDING]
        self.assertEqual(value, cookie_module.binding_of(new))
        self.assertTrue(attributes.get("secure"))
        self.assertTrue(after["session_id"][1].get("secure"), sent)

    def test_the_rotation_every_three_hours_binds_the_next_session(self):
        """Odoo gives a signed-in session a new id once it is three hours
        old (``_save_session``, a soft rotation); its binding comes with it."""
        _anonymous, own, cookies = self._sign_in_with_the_form(
            "tossed-victim", "tossed-victim-pw",
        )
        store = odoo.http.root.session_store
        session = store.get(own)
        session["create_time"] = time.time() - odoo.http.SESSION_ROTATION_INTERVAL - 60
        store.save(session)
        after = _set_cookies(
            self._session_info(f"session_id={own}; {BINDING}={cookies[BINDING][0]}"),
        )
        rotated = after["session_id"][0]
        self.assertNotEqual(rotated, own)
        bind = after[BINDING][0]
        self.assertEqual(bind, cookie_module.binding_of(rotated))
        self.assertEqual(
            self._uid(f"session_id={rotated}; {BINDING}={bind}"), self.victim.id,
        )

    def test_a_session_tossed_alone_does_not_sign_anybody_in(self):
        """The visitor without a session of the panel is not put into the
        account of whoever set the cookie (login CSRF)."""
        tossed = self.authenticate("tossed-attacker", "tossed-attacker-pw").sid
        self.assertIsNone(self._uid(f"session_id={tossed}"))

    def test_over_plain_http_the_session_cookie_is_odoo_s(self):
        """Without TLS there is no ``__Host-`` cookie, so nothing changes:
        a development panel on plain HTTP keeps working."""
        sid = self.authenticate("tossed-attacker", "tossed-attacker-pw").sid
        self.assertEqual(
            self._uid(f"session_id={sid}", secure=False), self.attacker.id,
        )


@tagged("-at_install", "post_install")
class TestTheWebsocketHandshake(_OnTheUncorrectedProxyFix, WebsocketCase):
    """``/websocket`` behind the proxy, which forwards it as ``wss``."""

    def setUp(self):
        super().setUp()
        self.startPatcher(patch.dict(config.options, {"proxy_mode": True}))
        new_test_user(
            self.env, login="ws-victim", password="ws-victim-pw",
            groups="base.group_user",
        )
        new_test_user(
            self.env, login="ws-attacker", password="ws-attacker-pw",
            groups="base.group_user",
        )

    def _handshake_uid(self, cookie):
        """Open a websocket with *cookie* the way Traefik forwards one.

        :returns: the user the handshake was served as, ``None`` for an
            anonymous session.
        """
        seen = {}
        opened = WebsocketConnectionHandler.open_connection

        def open_connection(request, version):
            """Note who the handshake is served as, then open it as Odoo does."""
            seen["uid"] = request.session.uid
            seen["secure"] = request.httprequest.is_secure
            return opened(request, version)

        with patch.object(WebsocketConnectionHandler, "open_connection", open_connection):
            self.websocket_connect(cookie=cookie, header={
                "X-Forwarded-Host": self.base_url().split("://", 1)[1],
                "X-Forwarded-Proto": "wss",
            })
        self.assertTrue(seen["secure"], "wss is not taken as HTTPS")
        return seen["uid"]

    def test_the_bound_session_is_the_websocket_s(self):
        tossed = self.authenticate("ws-attacker", "ws-attacker-pw").sid
        # Kept: authenticate() deletes the session it gave last.
        self.session = None
        own = self.authenticate("ws-victim", "ws-victim-pw")
        bind = cookie_module.binding_of(own.sid)
        self.assertEqual(
            self._handshake_uid(f"session_id={tossed}; session_id={own.sid}; {BINDING}={bind}"),
            own.uid,
        )

    def test_a_session_tossed_alone_is_not_the_websocket_s(self):
        tossed = self.authenticate("ws-attacker", "ws-attacker-pw").sid
        self.assertIsNone(self._handshake_uid(f"session_id={tossed}"))
