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
import re
from unittest.mock import patch

import requests
from werkzeug.test import EnvironBuilder

import odoo.http
from odoo.tests import new_test_user, tagged
from odoo.tests.common import BaseCase, HttpCase
from odoo.tools import config

from odoo.addons.incubacloud.models import http_session_cookie as cookie_module

from .test_real_ip import _OnTheUncorrectedProxyFix

#: The cookie that names the panel's own session.
BINDING = "__Host-session_bind"
CHROME = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
        self.startPatcher(patch.dict(config.options.maps[0], {"proxy_mode": True}))
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
        with self.allow_requests(all_requests=True):
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

    def _uid(self, cookie, secure=True):
        """Return the user a request with *cookie* is served as.

        :returns: the user id, or ``None`` when the request has no
            signed-in session (Odoo answers ``SessionExpired``, code 100).
        """
        answer = self._open(
            "/web/session/get_session_info",
            cookie=cookie,
            secure=secure,
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}),
            headers={"Content-Type": "application/json"},
        ).json()
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
        self.assertIsNone(odoo.http.root.session_store.get(anonymous).get("uid"))
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
