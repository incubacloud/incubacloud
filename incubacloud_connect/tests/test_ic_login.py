"""``/ic/login`` opens a session of its own, whatever the browser brings.

The link of "Connect as user" may be opened in a browser that is logged
in as someone else. Odoo caches ``_compute_session_token`` by session id
alone, so a login that kept the browser's sid stored the token the
worker had cached for the previous user: the other workers computed the
right one and logged the session out (``SessionExpiredException``), and
the browser chose the sid of the new session (session fixation).
Clearing the registry cache stands for "the next request reaches
another worker".
"""
import json
import os
import secrets
import time
from pathlib import Path
from unittest.mock import patch

from odoo import http
from odoo.service import security
from odoo.tests import get_db_name, new_test_user, tagged
from odoo.tests.common import HOST, HttpCase

from odoo.addons.incubacloud_connect.controllers import connect


@tagged('-at_install', 'post_install')
class TestIcLogin(HttpCase):
    """The link logs its user in on a new sid, with a token for it."""

    # ``auth='none'`` routes get a read-only cursor, and the route opens
    # a read/write cursor of its own on the link's database: a separate
    # connection on an instance, refused between test cursors only.
    # Odoo 18's TestCursor refuses it in ``__init__`` after taking the
    # registry's test lock, which it then never releases: the next
    # request of the class waits on it forever. Request cursors are
    # read/write here instead, as Odoo's own tests do.
    readonly_enabled = False

    def setUp(self):
        super().setUp()
        self.user_a = new_test_user(
            self.env, login='ic_login_a', groups='base.group_user',
        )
        self.user_b = new_test_user(
            self.env, login='ic_login_b', groups='base.group_user',
        )

    def authenticate(self, user, password, **kwargs):
        """Log in as ``HttpCase.authenticate``, the cookie on the test host.

        Odoo 18 sets the session cookie with no domain, and ``/ic/login``
        answers with one for ``HOST``: the opener keeps both and sends the
        old sid first, which the next request finds deleted. A browser
        keeps one cookie per name, domain and path, and Odoo 19 sets it
        for ``HOST`` too.

        :param str user: the login, or ``None`` for an anonymous session.
        :param str password: its password.
        :returns: the session ``HttpCase.authenticate`` returns.
        """
        session = super().authenticate(user, password, **kwargs)
        self.opener.cookies.set('session_id', None)
        self.opener.cookies.set('session_id', session.sid, domain=HOST)
        return session

    def _link(self, uid):
        """Write a one-time token for *uid*, as the panel does.

        :param int uid: the user the link logs in.
        :returns: the URL of the link.
        :rtype: str
        """
        token = secrets.token_hex(16)
        token_dir = Path(connect._TOKEN_DIR)
        token_dir.mkdir(mode=0o700, exist_ok=True)
        path = token_dir / token
        path.write_text(json.dumps({
            'ts': time.time(), 'uid': uid, 'db': get_db_name(), 'by': 'test',
        }))
        os.chmod(path, 0o600)
        self.addCleanup(path.unlink, missing_ok=True)
        return f'/ic/login?t={token}'

    def _open_link(self):
        """Open a link for B and return the sid the response hands out.

        :returns: the ``session_id`` cookie the response sets.
        :rtype: str
        """
        response = self.url_open(
            self._link(self.user_b.id), allow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        sid = response.cookies.get('session_id')
        self.assertTrue(sid, 'the login sets a session cookie')
        return sid

    def _assert_logged_in_as_b(self, sid):
        """Check that *sid* is B's session, valid on any worker.

        :param str sid: the session id the link handed out.
        """
        session = http.root.session_store.get(sid)
        self.assertEqual(session.uid, self.user_b.id)
        self.assertEqual(session.login, self.user_b.login)
        self.env.registry.clear_cache()
        self.assertTrue(
            security.check_session(session, self.env),
            'the session token is the one any worker computes for B',
        )
        # The next request, with the cookie the link set, is B's.
        self.env.registry.clear_cache()
        info = self.make_jsonrpc_request('/web/session/get_session_info')
        self.assertEqual(info['uid'], self.user_b.id)

    def test_a_session_of_another_user_is_not_reused(self):
        """Logged in as A, the link for B gives B a new sid.

        The session token of A's sid is in the registry cache, as on the
        worker that checked A's last request.
        """
        session_a = self.authenticate('ic_login_a', 'ic_login_a')
        old_sid = session_a.sid
        self.assertTrue(security.check_session(session_a, self.env))

        new_sid = self._open_link()

        self.assertNotEqual(new_sid, old_sid)
        old = http.root.session_store.get(old_sid)
        self.assertNotEqual(old.get('uid'), self.user_b.id)
        self.assertFalse(old.get('uid'), "A's session is gone, not B's")
        self._assert_logged_in_as_b(new_sid)

    def test_an_anonymous_session_is_not_reused(self):
        """A browser with a session nobody logged in gets a new sid."""
        anonymous = self.authenticate(None, None)

        new_sid = self._open_link()

        self.assertNotEqual(new_sid, anonymous.sid)
        self._assert_logged_in_as_b(new_sid)

    def test_a_browser_without_session(self):
        """A browser with no cookie at all is logged in as before.

        With no session cookie, Odoo picks the database only when there
        is a single one, as on an instance.
        """
        self.opener.cookies.clear()
        with patch.object(
            http, 'db_list', autospec=True, return_value=[get_db_name()],
        ):
            new_sid = self._open_link()
        self._assert_logged_in_as_b(new_sid)

    def test_the_session_takes_the_context_of_the_user_of_the_link(self):
        """Logged in as A in Spanish, the link for B gives B's own context.

        The session kept A's context (language, timezone, uid): B saw the
        panel in A's language and dates in A's timezone. It is now what
        Odoo's own login gives B: ``context_get`` as B.
        """
        self.env['res.lang']._activate_lang('es_ES')
        self.user_a.write({'lang': 'es_ES', 'tz': 'Europe/Madrid'})
        self.user_b.write({'lang': 'en_US', 'tz': 'America/New_York'})
        session_a = self.authenticate('ic_login_a', 'ic_login_a')
        self.assertEqual(session_a.context.get('lang'), 'es_ES')

        new_sid = self._open_link()

        session = http.root.session_store.get(new_sid)
        expected = dict(self.user_b.with_user(self.user_b).context_get())
        self.assertEqual(session.context, expected)
        self.assertEqual(session.context['lang'], 'en_US')
        self.assertEqual(session.context['tz'], 'America/New_York')
        self.assertEqual(session.context['uid'], self.user_b.id)
        self._assert_logged_in_as_b(new_sid)
        info = self.make_jsonrpc_request('/web/session/get_session_info')
        self.assertEqual(info['user_context']['lang'], 'en_US')
        self.assertEqual(info['user_context']['tz'], 'America/New_York')

    def test_the_context_is_the_one_odoo_s_login_gives(self):
        """The same as ``Session.finalize`` of this version stores."""
        self.user_b.write({'tz': 'Asia/Tokyo'})
        env_b = self.env(user=self.user_b)
        self.assertEqual(
            connect._user_context(env_b, self.user_b.id, None),
            dict(env_b['res.users'].context_get()),
        )

    def test_the_link_still_works_once(self):
        """The fix keeps the token single-use."""
        self.authenticate(None, None)
        link = self._link(self.user_b.id)
        self.assertEqual(
            self.url_open(link, allow_redirects=False).status_code, 303,
        )
        self.assertEqual(
            self.url_open(link, allow_redirects=False).status_code, 400,
        )
