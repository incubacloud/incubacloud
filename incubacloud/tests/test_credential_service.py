"""
Tier 2 — ORM integration tests for cloud.github.credential.service.

Tests PAT storage/retrieval and the App→PAT fallback pattern.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestCredentialServicePAT(TransactionCase):

    def _svc(self):
        return self.env['cloud.github.credential.service']

    def _settings(self):
        return self.env['cloud.settings']._get_system()

    def test_get_pat_returns_none_when_not_set(self):
        self._settings().write({'github_pat': ''})
        self.assertIsNone(self._svc().get_pat())

    def test_get_pat_returns_token_when_set(self):
        self._settings().write({'github_pat': 'ghp_test123'})
        self.assertEqual(self._svc().get_pat(), 'ghp_test123')

    def test_get_pat_strips_whitespace(self):
        self._settings().write({'github_pat': '  ghp_abc  '})
        self.assertEqual(self._svc().get_pat(), 'ghp_abc')

    def test_get_pat_empty_whitespace_returns_none(self):
        self._settings().write({'github_pat': '   '})
        self.assertIsNone(self._svc().get_pat())

    def test_pat_survives_app_reset(self):
        """PAT is stored in cloud.settings, not in cloud.github.app."""
        self._settings().write({'github_pat': 'ghp_persistent'})
        # Delete all github app records
        self.env['cloud.github.app'].sudo().search([]).unlink()
        # PAT still available
        self.assertEqual(self._svc().get_pat(), 'ghp_persistent')


class TestCredentialServiceGetCredentials(TransactionCase):

    def _svc(self):
        return self.env['cloud.github.credential.service']

    def test_raises_when_no_app_configured(self):
        self.env['cloud.github.app'].sudo().search([]).unlink()
        with self.assertRaises(UserError):
            self._svc().get_credentials()

    def test_returns_credentials_when_app_exists(self):
        # Ensure an app exists
        app = self.env['cloud.github.app'].sudo().search([], limit=1)
        if not app:
            app = self.env['cloud.github.app'].sudo().create({
                'app_id': 12345,
                'installation_id': 67890,
                'private_key': 'test-key',
            })
        creds = self._svc().get_credentials()
        self.assertTrue(creds)


class TestCredentialServiceTestConnection(TransactionCase):

    def _svc(self):
        return self.env['cloud.github.credential.service']

    def test_returns_error_when_no_app(self):
        self.env['cloud.github.app'].sudo().search([]).unlink()
        result = self._svc().test_connection()
        self.assertFalse(result['ok'])
        self.assertIn('not configured', result['error'].lower())
