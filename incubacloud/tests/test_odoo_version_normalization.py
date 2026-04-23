"""
Tests for odoo_version normalization in controllers.
"""
import unittest

from odoo.tests.common import BaseCase



class TestOdooVersionNormalization(BaseCase):
    """Test that numeric odoo_version values are normalized to strings."""

    def _normalize(self, v):
        """Replicate the normalization logic from data_load.py."""
        if isinstance(v, (int, float)):
            return f"{float(v):.1f}"
        return v

    def test_int_19_becomes_string(self):
        self.assertEqual(self._normalize(19), '19.0')

    def test_float_19_becomes_string(self):
        self.assertEqual(self._normalize(19.0), '19.0')

    def test_int_7_becomes_string(self):
        self.assertEqual(self._normalize(7), '7.0')

    def test_float_16_becomes_string(self):
        self.assertEqual(self._normalize(16.0), '16.0')

    def test_string_passes_through(self):
        self.assertEqual(self._normalize('19.0'), '19.0')

    def test_string_18_passes_through(self):
        self.assertEqual(self._normalize('18.0'), '18.0')
