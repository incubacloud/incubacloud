"""
Tier 2 — ORM integration tests for cloud.github.app.
"""
import http.client
import io
import json
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase
from odoo.exceptions import UserError

from odoo.addons.incubacloud.controllers._data_load import _routes_github
from odoo.addons.incubacloud.github.client import GitHubAppClient
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
            {'app_id': '12345', 'private_key': _FAKE_PEM,
             'owner_login': 'operator'} | kw
        )

    def test_sets_installation_id_when_empty(self):
        """The account that owns the App installed it: taken at once."""
        app = self._create_app()
        payload = {'installation': {'id': 987654, 'account': {'login': 'Operator'}}}
        result = self.App._process_installation_event(payload)
        self.assertTrue(result)
        self.assertEqual(app.installation_id, '987654')

    def test_a_stranger_s_installation_is_not_taken(self):
        """The App is public: another account's installation waits for a
        manager to choose it (Check installation in Settings)."""
        app = self._create_app()
        payload = {'installation': {'id': 777, 'account': {'login': 'stranger'}}}
        self.assertFalse(self.App._process_installation_event(payload))
        self.assertFalse(app.installation_id)

    def test_an_installation_without_its_account_is_not_taken(self):
        app = self._create_app()
        self.assertFalse(
            self.App._process_installation_event({'installation': {'id': 777}})
        )
        self.assertFalse(app.installation_id)

    def test_nothing_is_taken_while_the_app_s_owner_is_unknown(self):
        """An App set up by hand, or before the panel kept its owner."""
        app = self._create_app(owner_login=False)
        payload = {'installation': {'id': 777, 'account': {'login': 'operator'}}}
        self.assertFalse(self.App._process_installation_event(payload))
        self.assertFalse(app.installation_id)

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
        self.App._process_installation_event(
            {'installation': {'id': 42, 'account': {'login': 'operator'}}}
        )
        self.assertEqual(app.installation_id, '42')
        self.assertIsInstance(app.installation_id, str)


class TestInstallationOfSomeoneElse(TransactionCase):
    """The App is public: anyone may install it on their own account."""

    def setUp(self):
        super().setUp()
        self.env['cloud.github.app'].search([]).unlink()
        self.app = self.env['cloud.github.app'].create({
            'app_id': '12345', 'private_key': _FAKE_PEM,
            'installation_id': '4242', 'owner_login': 'operator',
        })

    def _dispatch(self, action, installation_id, account='stranger'):
        """Dispatch an ``installation`` event for *installation_id* on
        *account*."""
        event = self.env['cloud.github.event'].create({
            'event_type': 'installation',
            'action': action,
            'delivery_id': f'{action}-{installation_id}-{account}',
            'payload': '{}',
        })
        event._dispatch({'installation': {
            'id': installation_id, 'account': {'login': account},
        }})
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
        self._dispatch('created', 5151, account='operator')
        self.assertEqual(self.app.installation_id, '5151')
        self.assertFalse(self._revocation_alert())

    def test_a_stranger_installing_first_is_not_the_panel_s(self):
        """While the panel has no installation, a stranger's created is not
        adopted: it neither becomes the panel's nor clears its alert, and
        the operator's own installation, after it, still is taken."""
        self._dispatch('deleted', 4242)
        self._dispatch('created', 9999)
        self.assertFalse(self.app.installation_id)
        self.assertTrue(self._revocation_alert())
        self._dispatch('suspend', 9999)
        self._dispatch('deleted', 9999)
        self._dispatch('created', 5151, account='operator')
        self.assertEqual(self.app.installation_id, '5151')


class TestManagerChoosesTheInstallation(TransactionCase):
    """Check installation (the detection route) is how a manager takes an
    installation the webhook did not: it names the account and, when it
    takes one, the panel's revocation alert closes."""

    def setUp(self):
        super().setUp()
        self.env['cloud.github.app'].search([]).unlink()
        self.app = self.env['cloud.github.app'].create({
            'app_id': '12345', 'private_key': _FAKE_PEM,
            'owner_login': 'operator',
        })
        self.env['cloud.alert'].sudo().raise_alert(
            GITHUB_APP_REVOKED_CODE, 'The installation was deleted.',
            level='critical',
        )
        self.controller = _routes_github.GitHubMixin()
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _detect(self, installations):
        """Run the detection with GitHub answering *installations*."""
        client = MagicMock(spec=GitHubAppClient)
        client.list_installations.return_value = installations
        fake = MagicMock(spec=Request)
        fake.env = self.env
        Service = type(self.env['cloud.github.credential.service'])
        with patch.object(_routes_github, 'request', fake), patch.object(
            _routes_github, 'GitHubAppClient', return_value=client,
        ), patch.object(Service, 'get_credentials', return_value=object()):
            return self.controller.cloud_detect_github_installation()

    def _alert(self):
        return self.env['cloud.alert'].search([
            ('code', '=', GITHUB_APP_REVOKED_CODE), ('state', '=', 'active'),
        ])

    def test_the_only_installation_is_taken_and_the_alert_closes(self):
        result = self._detect([{'id': '5151', 'account_login': 'acme-org'}])
        self.assertTrue(result['auto_updated'])
        self.assertEqual(result['account'], 'acme-org')
        self.assertEqual(self.app.installation_id, '5151')
        self.assertFalse(self._alert())

    def test_several_installations_wait_for_the_manager(self):
        result = self._detect([
            {'id': '5151', 'account_login': 'acme-org'},
            {'id': '9999', 'account_login': 'stranger'},
        ])
        self.assertFalse(result['auto_updated'])
        self.assertFalse(self.app.installation_id)
        self.assertTrue(self._alert())


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
