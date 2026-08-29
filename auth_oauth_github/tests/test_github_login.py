"""Tests for the GitHub authorization code login.

The GitHub API is mocked with ``MagicMock(spec=requests.Response)`` so an
attribute the real response class does not have fails here instead of in
production.
"""
from unittest.mock import MagicMock, patch

import requests

from odoo.exceptions import AccessDenied
from odoo.tests.common import TransactionCase

from odoo.addons.auth_oauth_github.controllers import main as github_main

_MODULE = 'odoo.addons.auth_oauth_github.controllers.main'


def _response(payload):
    """Return a mocked ``requests.Response`` decoding to *payload*."""
    resp = MagicMock(spec=requests.Response)
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class TestPickVerifiedEmail(TransactionCase):
    """The email is the whole trust boundary — pin its selection."""

    def test_primary_verified_wins(self):
        email = github_main._pick_verified_email([
            {'email': 'other@example.test', 'primary': False, 'verified': True},
            {'email': 'main@example.test', 'primary': True, 'verified': True},
        ])
        self.assertEqual(email, 'main@example.test')

    def test_falls_back_to_any_verified(self):
        email = github_main._pick_verified_email([
            {'email': 'unverified@example.test', 'primary': True,
             'verified': False},
            {'email': 'verified@example.test', 'primary': False,
             'verified': True},
        ])
        self.assertEqual(email, 'verified@example.test')

    def test_unverified_primary_is_not_accepted(self):
        """An unverified address proves nothing about who is signing in."""
        self.assertIsNone(github_main._pick_verified_email([
            {'email': 'nope@example.test', 'primary': True, 'verified': False},
        ]))

    def test_empty_and_garbage_are_refused(self):
        self.assertIsNone(github_main._pick_verified_email([]))
        self.assertIsNone(github_main._pick_verified_email(None))
        self.assertIsNone(github_main._pick_verified_email(['not-a-dict']))


class TestGitHubIdentity(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = github_main.GitHubOAuthController()

    def test_identity_uses_numeric_id_and_verified_email(self):
        """The subject is the stable numeric id, never the username."""
        with patch(f'{_MODULE}.requests.get') as mock_get:
            mock_get.side_effect = [
                _response({'id': 4242, 'login': 'octocat', 'name': 'Octo Cat'}),
                _response([
                    {'email': 'octo@example.test', 'primary': True,
                     'verified': True},
                ]),
            ]
            validation = self.controller._read_identity('tok')

        self.assertEqual(validation['user_id'], '4242')
        self.assertEqual(validation['email'], 'octo@example.test')
        self.assertTrue(validation['email_verified'])
        self.assertEqual(validation['name'], 'Octo Cat')

    def test_api_calls_use_authorization_header(self):
        """GitHub rejects tokens sent as a query string, which is exactly
        how stock ``_auth_oauth_rpc`` would have sent them."""
        with patch(f'{_MODULE}.requests.get') as mock_get:
            mock_get.side_effect = [
                _response({'id': 1, 'login': 'a'}),
                _response([{'email': 'a@example.test', 'primary': True,
                            'verified': True}]),
            ]
            self.controller._read_identity('secret-token')

        for call in mock_get.call_args_list:
            headers = call.kwargs['headers']
            self.assertEqual(headers['Authorization'], 'Bearer secret-token')
            self.assertNotIn('params', call.kwargs)

    def test_no_verified_email_is_refused(self):
        with patch(f'{_MODULE}.requests.get') as mock_get:
            mock_get.side_effect = [
                _response({'id': 7, 'login': 'ghost'}),
                _response([{'email': 'ghost@example.test', 'primary': True,
                            'verified': False}]),
            ]
            with self.assertRaises(AccessDenied):
                self.controller._read_identity('tok')

    def test_missing_account_id_is_refused(self):
        with patch(f'{_MODULE}.requests.get') as mock_get:
            mock_get.side_effect = [_response({'login': 'no-id'})]
            with self.assertRaises(AccessDenied):
                self.controller._read_identity('tok')


class TestGitHubCodeExchange(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = github_main.GitHubOAuthController()
        self.provider = self.env['auth.oauth.provider'].create({
            'name': 'GitHub test',
            'github_flow': True,
            'enabled': True,
            'client_id': 'cid',
            'client_secret': 'csecret',
            'auth_endpoint': 'https://github.com/login/oauth/authorize',
            'validation_endpoint': 'https://api.github.com/user',
            'scope': 'read:user user:email',
            'body': 'Sign in with GitHub',
        })

    def test_exchange_requests_json_and_returns_token(self):
        """Without ``Accept: application/json`` GitHub answers this
        endpoint form-encoded and the response cannot be decoded."""
        with patch(f'{_MODULE}._callback_url', return_value='https://x/cb'), \
             patch(f'{_MODULE}.requests.post') as mock_post:
            mock_post.return_value = _response({'access_token': 'gho_abc'})
            token = self.controller._exchange_code(self.provider, 'the-code')

        self.assertEqual(token, 'gho_abc')
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs['headers']['Accept'], 'application/json')
        self.assertEqual(kwargs['data']['client_secret'], 'csecret')
        self.assertEqual(kwargs['data']['code'], 'the-code')
        self.assertEqual(kwargs['data']['redirect_uri'], 'https://x/cb')

    def test_error_payload_is_access_denied(self):
        with patch(f'{_MODULE}._callback_url', return_value='https://x/cb'), \
             patch(f'{_MODULE}.requests.post') as mock_post:
            mock_post.return_value = _response(
                {'error': 'bad_verification_code'},
            )
            with self.assertRaises(AccessDenied):
                self.controller._exchange_code(self.provider, 'stale')

    def test_missing_credentials_raise_before_any_call(self):
        self.provider.client_secret = False
        with patch(f'{_MODULE}.requests.post') as mock_post:
            with self.assertRaises(ValueError):
                self.controller._exchange_code(self.provider, 'code')
        mock_post.assert_not_called()


class TestGitHubProviderRecord(TransactionCase):

    def test_shipped_provider_is_disabled_and_credential_less(self):
        """Shipping it enabled would put a button on every login page that
        cannot work until someone pastes credentials."""
        provider = self.env.ref('auth_oauth_github.provider_github')
        self.assertTrue(provider.github_flow)
        self.assertFalse(provider.enabled)
        self.assertFalse(provider.client_id)
        self.assertFalse(provider.sudo().client_secret)
        self.assertIn('user:email', provider.scope)


class TestBuildAuthLink(TransactionCase):
    """The login button is what every visitor hits — pin its shape.

    A regression here is silent: the page still renders, the button still
    looks right, and the flow only breaks once someone clicks it.
    """

    PROVIDER = {
        'id': 7,
        'client_id': 'test-client-id',
        'auth_endpoint': 'https://github.com/login/oauth/authorize',
        'scope': 'read:user user:email',
    }

    def _link(self, **overrides):
        provider = self.PROVIDER | overrides
        return github_main.build_auth_link(
            provider, 'https://odoo.test/auth_oauth/github/callback',
            {'d': 'db', 'p': 7, 'r': '', 'n': 'the-nonce'},
        )

    def test_requests_a_code_not_a_token(self):
        """Stock builds ``response_type=token``; GitHub has no implicit
        flow, so a button left on stock's shape simply fails."""
        link = self._link()
        self.assertIn('response_type=code', link)
        self.assertNotIn('response_type=token', link)

    def test_points_at_this_modules_callback(self):
        link = self._link()
        self.assertIn(
            'redirect_uri=https%3A%2F%2Fodoo.test%2Fauth_oauth%2Fgithub'
            '%2Fcallback',
            link,
        )

    def test_carries_the_nonce_in_state(self):
        """Without it the callback would accept a state an attacker
        composed — the login CSRF the nonce exists to stop."""
        self.assertIn('the-nonce', self._link())

    def test_scope_defaults_to_user_email(self):
        """``user:email`` is what makes /user/emails readable; losing it
        would break every login with a private address."""
        self.assertIn('user%3Aemail', self._link(scope=False))

    def test_uses_the_configured_endpoint_and_client(self):
        link = self._link()
        self.assertTrue(
            link.startswith('https://github.com/login/oauth/authorize?'),
        )
        self.assertIn('client_id=test-client-id', link)
