"""
Tier 2 — controller test for the general-settings GitHub event retention
fields exposed on the Settings > General tab.

Covers that ``get_general_settings`` returns the two GitHub event
retention fields and that ``save_general_settings`` persists them onto
``cloud.settings``.
"""
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud


class TestGeneralSettingsGithubRetention(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        # The endpoints are manager-gated via ``self._sec()``. Stub it on
        # the isolated mixin so the gate resolves; under TransactionCase
        # the env is superuser so the checks pass.
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _fake_request(self):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        return fake_req

    def test_get_returns_github_event_fields(self):
        settings = self.env['cloud.settings'].sudo()._get()
        settings.write({
            'github_event_retention_days': 45,
            'github_event_truncate_days': 3,
        })
        with patch.object(_routes_crud, 'request', self._fake_request()):
            data = self.controller.cloud_get_general_settings()
        self.assertEqual(data['github_event_retention_days'], 45)
        self.assertEqual(data['github_event_truncate_days'], 3)

    def test_save_persists_github_event_fields(self):
        with patch.object(_routes_crud, 'request', self._fake_request()):
            result = self.controller.cloud_save_general_settings(
                github_event_retention_days=120,
                github_event_truncate_days=10,
            )
        self.assertTrue(result['ok'])
        settings = self.env['cloud.settings'].sudo()._get()
        self.assertEqual(settings.github_event_retention_days, 120)
        self.assertEqual(settings.github_event_truncate_days, 10)

    def test_save_clamps_negative_to_zero(self):
        with patch.object(_routes_crud, 'request', self._fake_request()):
            self.controller.cloud_save_general_settings(
                github_event_retention_days=-5,
                github_event_truncate_days=-1,
            )
        settings = self.env['cloud.settings'].sudo()._get()
        self.assertEqual(settings.github_event_retention_days, 0)
        self.assertEqual(settings.github_event_truncate_days, 0)
