"""
Tier 2 — ORM integration tests for cloud.github.app.
"""
import http.client
import io
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase, TransactionCase
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models.cloud_github_event import (
    GITHUB_APP_REVOKED_CODE,
)

_FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "ZmFrZWtleWZha2VrZXlmYWtla2V5ZmFrZWtleQ==\n"
    "-----END RSA PRIVATE KEY-----"
)


class TestCloudGitHubAppSingleton(TransactionCase):

    def setUp(self):
        super().setUp()
        self.App = self.env['cloud.github.app']
        # Remove any records installed by demo/data so each test is isolated
        self.App.search([]).unlink()

    def _create(self, **kw):
        return self.App.create(
            {'app_id': '111111', 'private_key': _FAKE_PEM} | kw
        )

    def test_create_first_record_succeeds(self):
        app = self._create()
        self.assertTrue(app.id)

    def test_create_second_record_raises_user_error(self):
        self._create(app_id='111')
        with self.assertRaises(UserError):
            self._create(app_id='222')

    def test_app_id_stored(self):
        app = self._create(app_id='999888')
        self.assertEqual(app.app_id, '999888')


class TestCloudGitHubAppInstallationEvent(TransactionCase):

    def setUp(self):
        super().setUp()
        self.App = self.env['cloud.github.app']
        self.App.search([]).unlink()

    def _create_app(self, **kw):
        return self.App.create(
            {'app_id': '12345', 'private_key': _FAKE_PEM} | kw
        )

    def test_sets_installation_id_when_empty(self):
        app = self._create_app()
        payload = {'installation': {'id': 987654}}
        result = self.App._process_installation_event(payload)
        self.assertTrue(result)
        self.assertEqual(app.installation_id, '987654')

    def test_noop_when_installation_id_already_set(self):
        app = self._create_app(installation_id='existing-id')
        payload = {'installation': {'id': 999999}}
        result = self.App._process_installation_event(payload)
        self.assertFalse(result)
        self.assertEqual(app.installation_id, 'existing-id')

    def test_returns_false_when_no_app_exists(self):
        payload = {'installation': {'id': 123}}
        result = self.App._process_installation_event(payload)
        self.assertFalse(result)

    def test_returns_false_when_payload_has_no_installation(self):
        self._create_app()
        result = self.App._process_installation_event({'action': 'created'})
        self.assertFalse(result)

    def test_returns_false_when_installation_id_is_empty(self):
        self._create_app()
        result = self.App._process_installation_event({'installation': {}})
        self.assertFalse(result)

    def test_installation_id_stored_as_string(self):
        app = self._create_app()
        self.App._process_installation_event({'installation': {'id': 42}})
        self.assertEqual(app.installation_id, '42')
        self.assertIsInstance(app.installation_id, str)


class TestInstallationOfSomeoneElse(TransactionCase):
    """The App is public: anyone may install it on their own account."""

    def setUp(self):
        super().setUp()
        self.env['cloud.github.app'].search([]).unlink()
        self.app = self.env['cloud.github.app'].create({
            'app_id': '12345', 'private_key': _FAKE_PEM,
            'installation_id': '4242',
        })

    def _dispatch(self, action, installation_id):
        """Dispatch an ``installation`` event for *installation_id*."""
        event = self.env['cloud.github.event'].create({
            'event_type': 'installation',
            'action': action,
            'delivery_id': f'{action}-{installation_id}',
            'payload': '{}',
        })
        event._dispatch({'installation': {'id': installation_id}})
        return event

    def _revocation_alert(self):
        return self.env['cloud.alert'].search([
            ('code', '=', GITHUB_APP_REVOKED_CODE), ('state', '=', 'active'),
        ])

    def test_a_stranger_deleting_theirs_raises_nothing(self):
        for action in ('deleted', 'suspend'):
            event = self._dispatch(action, 9999)
            self.assertTrue(event.processed)
        self.assertFalse(self._revocation_alert())
        self.assertEqual(self.app.installation_id, '4242')

    def test_a_stranger_installing_theirs_clears_no_real_alert(self):
        self._dispatch('suspend', 4242)
        self._dispatch('created', 9999)
        self._dispatch('unsuspend', 9999)
        self.assertTrue(self._revocation_alert())
        self.assertEqual(self.app.installation_id, '4242')

    def test_deleting_the_panels_own_lets_its_reinstall_in(self):
        self._dispatch('deleted', 4242)
        self.assertFalse(self.app.installation_id)
        self.assertTrue(self._revocation_alert())
        self._dispatch('created', 5151)
        self.assertEqual(self.app.installation_id, '5151')
        self.assertFalse(self._revocation_alert())


class TestTheScopedInstallationToken(BaseCase):
    """``GitHubAppClient.get_scoped_installation_token`` (10-nov-2026)."""

    def test_it_asks_for_the_repositories_and_reading_only_and_skips_the_cache(self):
        """The body limits the token; the installation's cache is untouched."""
        from odoo.addons.incubacloud.github import client as client_mod
        from odoo.addons.incubacloud.github.credentials import GitHubAppCredentials

        creds = GitHubAppCredentials(app_id="1", installation_id="77", private_key_pem="pem")
        resp = MagicMock(spec=http.client.HTTPResponse)
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(
            {"token": "ghs_scoped", "expires_at": "2026-11-10T10:00:00Z"},
        ).encode()
        with patch.object(client_mod, "generate_github_app_jwt", return_value="jwt"), \
                patch.object(client_mod, "safe_urlopen", return_value=resp) as opened, \
                patch.object(client_mod, "token_cache", autospec=True) as cache:
            token = client_mod.GitHubAppClient(creds).get_scoped_installation_token(
                [12, 11, 12],
            )
        self.assertEqual(token, "ghs_scoped")
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://api.github.com/app/installations/77/access_tokens",
        )
        self.assertEqual(json.loads(request.data), {
            "repository_ids": [11, 12],
            "permissions": {"contents": "read", "metadata": "read"},
        })
        self.assertEqual(request.get_header("Content-type"), "application/json")
        cache.get_token.assert_not_called()
        cache.set_token.assert_not_called()

    def test_a_refusal_raises(self):
        """GitHub's refusal reaches the caller, which refuses the job."""
        import urllib.error

        from odoo.addons.incubacloud.github import client as client_mod
        from odoo.addons.incubacloud.github.credentials import GitHubAppCredentials

        creds = GitHubAppCredentials(app_id="1", installation_id="77", private_key_pem="pem")
        error = urllib.error.HTTPError(
            "https://api.github.com", 422, "Unprocessable", {}, io.BytesIO(b"{}"),
        )
        with patch.object(client_mod, "generate_github_app_jwt", return_value="jwt"), \
                patch.object(client_mod, "safe_urlopen", side_effect=error), \
                self.assertRaises(client_mod.GitHubAPIError):
            client_mod.GitHubAppClient(creds).get_scoped_installation_token([1])
