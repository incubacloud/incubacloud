"""The GitHub App setup's ``state`` is checked without the session.

The state lived in the session, and Odoo rewrites a session whole when
it records a new device for it — behind the CDN, any request from
another edge address. One landing on another worker while the setup
page saved the state erased it: the customer came back from GitHub to
"invalid state" (QA, 12-oct-2026; 3 in 30 on three workers). The state
is now signed for the user who started the setup.
"""
import re

from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.incubacloud.controllers.github_setup import (
    _STATE_TTL_SECONDS,
    _make_state,
    _state_problem,
)

_NOW = 1_791_830_000


class TestSetupState(TransactionCase):

    def setUp(self):
        super().setUp()
        self.uid_a = self.env.ref('base.user_admin').id
        self.uid_b = self.env.ref('base.user_root').id

    def test_a_state_is_good_for_the_user_who_started_the_setup(self):
        state = _make_state(self.env, self.uid_a, now=_NOW)
        self.assertIsNone(_state_problem(self.env, self.uid_a, state, now=_NOW + 5))

    def test_another_user_cannot_bring_it_back(self):
        state = _make_state(self.env, self.uid_a, now=_NOW)
        self.assertEqual(
            _state_problem(self.env, self.uid_b, state, now=_NOW + 5),
            'invalid_state',
        )

    def test_a_state_that_was_not_signed_here_is_refused(self):
        issued, nonce, signature = _make_state(
            self.env, self.uid_a, now=_NOW,
        ).split('.')
        for forged in (
            f'{issued}.{nonce}x.{signature}',
            f'{int(issued) + 1}.{nonce}.{signature}',
            'IYf6gV5ae9HDloocHlUvEg',
            '',
            None,
        ):
            with self.subTest(state=forged):
                self.assertEqual(
                    _state_problem(self.env, self.uid_a, forged, now=_NOW + 5),
                    'invalid_state',
                )

    def test_an_old_state_has_expired(self):
        state = _make_state(self.env, self.uid_a, now=_NOW)
        self.assertEqual(
            _state_problem(
                self.env, self.uid_a, state, now=_NOW + _STATE_TTL_SECONDS + 1,
            ),
            'state_expired',
        )


@tagged('-at_install', 'post_install')
class TestSetupCallbackWithoutTheSession(HttpCase):

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'name': 'gh-setup',
            'login': 'gh-setup',
            'password': 'gh-setup',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('incubacloud.group_cloud_manager').id,
            ])],
        })
        # An app already there: the callback stops right after the state
        # check, before it would call GitHub.
        App = self.env['cloud.github.app'].sudo()
        App.search([]).unlink()
        App.create({'app_id': '12345', 'private_key': 'test-key'})

    def _get(self, path, **params):
        """GET *path* as the browser would behind the proxy."""
        return self.url_open(
            path,
            params=params,
            headers={'X-Forwarded-Proto': 'https'},
            allow_redirects=False,
        )

    def test_the_callback_needs_nothing_the_session_kept(self):
        self.authenticate('gh-setup', 'gh-setup')
        page = self._get('/cloud/github/setup')
        state = re.search(r'state=([A-Za-z0-9_.\-]+)', page.text).group(1)
        # A new session for the same user: what was in the old one is
        # gone, as when another worker rewrote it.
        self.authenticate('gh-setup', 'gh-setup')
        answer = self._get('/cloud/github/callback', code='x', state=state)
        self.assertIn('setup_error=app_exists', answer.headers['Location'])

    def test_a_state_from_nowhere_is_still_refused(self):
        self.authenticate('gh-setup', 'gh-setup')
        answer = self._get(
            '/cloud/github/callback', code='x', state='IYf6gV5ae9HDloocHlUvEg',
        )
        self.assertIn('setup_error=invalid_state', answer.headers['Location'])
