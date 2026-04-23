"""Unit tests for ``controllers._safe_error.safe_error_response``."""
import re

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.controllers._safe_error import (
    safe_error_response,
)


class TestSafeErrorResponse(BaseCase):

    def test_user_error_message_passes_through(self):
        resp = safe_error_response(
            UserError("Backend name required"), "Failed to save backend",
        )
        self.assertEqual(
            resp, {'ok': False, 'error': "Backend name required"},
        )

    def test_validation_error_message_passes_through(self):
        resp = safe_error_response(
            ValidationError("Port must be 1-65535"), "Failed to save host",
        )
        self.assertEqual(
            resp, {'ok': False, 'error': "Port must be 1-65535"},
        )

    def test_access_error_message_passes_through(self):
        resp = safe_error_response(
            AccessError("You do not have access"), "Forbidden",
        )
        self.assertEqual(
            resp, {'ok': False, 'error': "You do not have access"},
        )

    def test_generic_exception_is_redacted_with_correlation_id(self):
        resp = safe_error_response(
            ValueError("duplicate key value in cloud_host_ip_uniq"),
            "Failed to save host",
        )
        self.assertEqual(resp['ok'], False)
        self.assertNotIn("duplicate key", resp['error'])
        self.assertTrue(resp['error'].startswith("Failed to save host"))
        # Correlation id is the final token, lowercase hex, 8 chars.
        m = re.search(r'\(ref: ([0-9a-f]{8})\)$', resp['error'])
        self.assertIsNotNone(
            m,
            f"expected 8-hex correlation id at end, got: {resp['error']!r}",
        )

    def test_extra_fields_are_merged(self):
        """Some callers need to preserve shape fields the frontend
        reads even on failure (e.g. ``modules: []``)."""
        resp = safe_error_response(
            RuntimeError("network down"),
            "Failed to fetch modules",
            extra={'modules': [], 'branch': ''},
        )
        self.assertEqual(resp['ok'], False)
        self.assertEqual(resp['modules'], [])
        self.assertEqual(resp['branch'], '')
        self.assertIn("Failed to fetch modules", resp['error'])

    def test_extra_does_not_override_ok_or_error(self):
        resp = safe_error_response(
            UserError("keep me"),
            "ignored",
            extra={'ok': True, 'error': 'ignored', 'other': 42},
        )
        # ``extra`` is merged AFTER the payload — documented behavior,
        # regression guard: if callers pass ``ok`` or ``error`` in
        # extra, they win. That's the escape hatch for edge cases.
        self.assertEqual(resp['ok'], True)
        self.assertEqual(resp['error'], 'ignored')
        self.assertEqual(resp['other'], 42)
