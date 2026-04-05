"""
Tier 2 — ORM integration tests for cloud.github.app.
"""
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError

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
