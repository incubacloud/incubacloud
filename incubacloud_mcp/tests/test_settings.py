"""Where the MCP keeps its key, its URL, its issuer and its limit."""

from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.models.cloud_settings import (
    RESOURCE_PARAM,
    SECRET_PARAM,
)


class TestMcpSettings(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Settings = self.env["cloud.settings"]
        self.ICP = self.env["ir.config_parameter"].sudo()

    def test_the_provider_records_its_issuer_and_shows_it(self):
        provider = self.env["auth.oauth.provider"].create(
            {
                "name": "MCP test IdP",
                "client_id": "x",
                "body": "x",
                "auth_endpoint": "https://idp.example.test/oauth/authorize",
                "validation_endpoint": "https://idp.example.test/oauth/userinfo",
                "issuer": "https://idp.example.test",
            }
        )
        self.assertEqual(provider.issuer, "https://idp.example.test")
        views = self.env["auth.oauth.provider"].get_views([(False, "form")])
        self.assertIn('name="issuer"', views["views"]["form"]["arch"])

    def test_the_module_has_a_key_and_a_resource_url(self):
        self.assertEqual(len(self.ICP.get_param(SECRET_PARAM) or ""), 64)
        self.assertTrue(self.ICP.get_param(RESOURCE_PARAM))

    def test_the_key_is_created_when_missing(self):
        self.ICP.set_param(SECRET_PARAM, False)
        key = self.Settings._mcp_secret()
        self.assertEqual(len(key), 32)
        self.assertEqual(bytes.fromhex(self.ICP.get_param(SECRET_PARAM)), key)

    def test_the_resource_defaults_to_web_base_url_in_canonical_form(self):
        self.ICP.set_param(RESOURCE_PARAM, False)
        self.ICP.set_param("web.base.url", "HTTPS://Panel.Example.TEST:443/")
        self.assertEqual(
            self.Settings._mcp_resource(), "https://panel.example.test/mcp"
        )
        self.Settings._mcp_install_defaults()
        self.assertEqual(
            self.ICP.get_param(RESOURCE_PARAM), "https://panel.example.test/mcp"
        )

    def test_a_resource_already_set_is_kept(self):
        self.ICP.set_param(RESOURCE_PARAM, "https://acme.example.test/mcp")
        self.Settings._mcp_install_defaults()
        self.assertEqual(self.Settings._mcp_resource(), "https://acme.example.test/mcp")

    def test_the_user_cap_falls_back_to_its_default(self):
        settings = self.Settings._get_system()
        settings.rate_limit_mcp_user_per_min = 0
        self.assertEqual(self.Settings._mcp_user_cap(), 120)
        settings.rate_limit_mcp_user_per_min = 30
        self.assertEqual(self.Settings._mcp_user_cap(), 30)

    def test_the_issuer_must_be_an_https_url(self):
        settings = self.Settings._get_system()
        for bad in (
            "http://idp.example.test",
            "idp.example.test",
            "https://idp.example.test/x?y=1",
            "https://user:pw@idp.example.test",
            "https://@idp.example.test",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                settings.mcp_oauth_issuer = bad
                settings.flush_recordset()
        settings.mcp_oauth_issuer = "https://idp.example.test"
        self.assertEqual(self.Settings._mcp_issuer(), "https://idp.example.test")

    def test_a_self_hosted_panel_is_its_own_master(self):
        self.assertTrue(self.Settings._mcp_api_keys_allowed())
        self.assertFalse(self.Settings._mcp_settings_managed())
        self.assertEqual(self.Settings._mcp_agents_url(), "")

    def test_a_layer_above_can_say_otherwise(self):
        with patch.object(
            type(self.Settings),
            "_mcp_api_keys_allowed",
            autospec=True,
            return_value=False,
        ):
            self.assertFalse(self.Settings._mcp_api_keys_allowed())
