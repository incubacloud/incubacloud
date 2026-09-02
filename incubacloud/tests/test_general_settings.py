"""
Tier 2 — controller test for the general-settings GitHub event retention
fields exposed on the Settings > General tab.

Covers that ``get_general_settings`` returns the two GitHub event
retention fields and that ``save_general_settings`` persists them onto
``cloud.settings``.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
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


class TestGeneralSettingsContainerLogRotation(TransactionCase):
    """The two container-log knobs travel through the same endpoints."""

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _fake_request(self):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        return fake_req

    def test_get_returns_container_log_fields(self):
        settings = self.env['cloud.settings'].sudo()._get()
        settings.write({
            'container_log_max_size': '20m',
            'container_log_max_file': 4,
        })
        with patch.object(_routes_crud, 'request', self._fake_request()):
            data = self.controller.cloud_get_general_settings()
        self.assertEqual(data['container_log_max_size'], '20m')
        self.assertEqual(data['container_log_max_file'], 4)

    def test_save_persists_container_log_fields(self):
        with patch.object(_routes_crud, 'request', self._fake_request()):
            result = self.controller.cloud_save_general_settings(
                container_log_max_size='50m',
                container_log_max_file=5,
            )
        self.assertTrue(result['ok'])
        settings = self.env['cloud.settings'].sudo()._get()
        self.assertEqual(settings.container_log_max_size, '50m')
        self.assertEqual(settings.container_log_max_file, 5)

    def test_save_normalises_size_case_and_whitespace(self):
        with patch.object(_routes_crud, 'request', self._fake_request()):
            self.controller.cloud_save_general_settings(
                container_log_max_size='  50M ',
            )
        settings = self.env['cloud.settings'].sudo()._get()
        self.assertEqual(settings.container_log_max_size, '50m')

    def test_save_without_the_fields_leaves_them_alone(self):
        """A caller that predates the knobs cannot blank them out."""
        settings = self.env['cloud.settings'].sudo()._get()
        settings.write({
            'container_log_max_size': '30m',
            'container_log_max_file': 6,
        })
        with patch.object(_routes_crud, 'request', self._fake_request()):
            self.controller.cloud_save_general_settings(
                job_retention_days=180,
            )
        self.assertEqual(settings.container_log_max_size, '30m')
        self.assertEqual(settings.container_log_max_file, 6)

    def test_save_rejects_a_malformed_size(self):
        """The model constraint is the gate; the endpoint lets it speak."""
        with patch.object(_routes_crud, 'request', self._fake_request()), \
                self.assertRaises(ValidationError):
            self.controller.cloud_save_general_settings(
                container_log_max_size='lots',
            )


class TestGeneralSettingsLogBounds(TransactionCase):
    """The three cost bounds travel through the General endpoints."""

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _fake_request(self):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        return fake_req

    def test_get_returns_the_bounds(self):
        settings = self.env['cloud.settings'].sudo()._get()
        settings.write({
            'log_download_max_mb': 32,
            'log_search_max_files': 20,
            'log_search_timeout_s': 15,
        })
        with patch.object(_routes_crud, 'request', self._fake_request()):
            data = self.controller.cloud_get_general_settings()
        self.assertEqual(data['log_download_max_mb'], 32)
        self.assertEqual(data['log_search_max_files'], 20)
        self.assertEqual(data['log_search_timeout_s'], 15)

    def test_save_persists_the_bounds(self):
        with patch.object(_routes_crud, 'request', self._fake_request()):
            result = self.controller.cloud_save_general_settings(
                log_download_max_mb=16,
                log_search_max_files=10,
                log_search_timeout_s=20,
            )
        self.assertTrue(result['ok'])
        settings = self.env['cloud.settings'].sudo()._get()
        self.assertEqual(settings.log_download_max_mb, 16)
        self.assertEqual(settings.log_search_max_files, 10)
        self.assertEqual(settings.log_search_timeout_s, 20)


class TestCoreRateLimitsCoverLogAccess(TransactionCase):
    """Log and GitHub import caps live together on the Rates tab."""

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _fake_request(self):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        return fake_req

    def test_get_returns_the_log_caps(self):
        """GET exposes both minute-based log caps."""
        with patch.object(_routes_crud, 'request', self._fake_request()):
            data = self.controller.cloud_get_core_rate_limits()
        self.assertIn('rate_limit_logs_per_min', data)
        self.assertIn('rate_limit_log_search_per_min', data)

    def test_save_persists_the_log_caps(self):
        """Save persists both minute-based log caps."""
        with patch.object(_routes_crud, 'request', self._fake_request()):
            self.controller.cloud_save_core_rate_limits({
                'rate_limit_logs_per_min': 45,
                'rate_limit_log_search_per_min': 3,
            })
        settings = self.env['cloud.settings'].sudo()._get()
        self.assertEqual(settings.rate_limit_logs_per_min, 45)
        self.assertEqual(settings.rate_limit_log_search_per_min, 3)

    def test_get_returns_the_hourly_github_caps(self):
        """GET exposes preview and import as separate hourly values."""
        with patch.object(_routes_crud, 'request', self._fake_request()):
            data = self.controller.cloud_get_core_rate_limits()
        self.assertEqual(data['rate_limit_github_previews_per_hour'], 10)
        self.assertEqual(data['rate_limit_github_imports_per_hour'], 5)

    def test_save_persists_the_hourly_github_caps(self):
        """Both hourly thresholds are configurable without redeploying."""
        with patch.object(_routes_crud, 'request', self._fake_request()):
            self.controller.cloud_save_core_rate_limits({
                'rate_limit_github_previews_per_hour': 17,
                'rate_limit_github_imports_per_hour': 8,
            })
        settings = self.env['cloud.settings'].sudo()._get()
        self.assertEqual(settings.rate_limit_github_previews_per_hour, 17)
        self.assertEqual(settings.rate_limit_github_imports_per_hour, 8)
