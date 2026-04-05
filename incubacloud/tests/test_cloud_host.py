"""
Tier 2 — ORM integration tests for cloud.host.
Verifies Traefik template defaults and required fields.
"""
from odoo.tests.common import TransactionCase


class TestCloudHostTraefikDefaults(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Host = self.env['cloud.host']

    def _create(self, **kw):
        return self.Host.create({
            'name': 'Test Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'example.com',
        } | kw)

    def test_create_applies_defaults(self):
        """Host without traefik fields gets templates."""
        host = self._create()
        self.assertTrue(host.traefik_config_yml)
        self.assertTrue(host.traefik_inverseproxy_yaml)
        self.assertTrue(host.traefik_yml)

    def test_create_preserves_explicit(self):
        """Explicit traefik fields are not overwritten."""
        custom = "# custom config"
        host = self._create(traefik_config_yml=custom)
        self.assertEqual(host.traefik_config_yml, custom)

    def test_create_empty_string_gets_defaults(self):
        """Empty strings from frontend get replaced."""
        host = self._create(
            traefik_config_yml='',
            traefik_inverseproxy_yaml='',
            traefik_yml='',
        )
        self.assertTrue(host.traefik_config_yml)
        self.assertTrue(host.traefik_inverseproxy_yaml)
        self.assertTrue(host.traefik_yml)

    def test_defaults_contain_traefik(self):
        """Templates contain recognizable Traefik config."""
        host = self._create()
        self.assertIn('traefik', host.traefik_yml.lower())
        self.assertIn(
            'traefik',
            host.traefik_inverseproxy_yaml.lower(),
        )
