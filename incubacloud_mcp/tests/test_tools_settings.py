"""Settings: the MCP's own routes, and changing one field without resetting others."""

import json
from unittest.mock import patch

from odoo import http
from odoo.http import request
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import McpHttpMixin, McpUsersMixin, PanelFixtureMixin


class _ApprovalProbe(http.Controller):
    """What update_settings would ask the user to approve (test only)."""

    @http.route("/cloud/mcp_test/approval_text", type="jsonrpc", auth="user")
    def mcp_test_approval_text(self, args):
        """Return the approval text of update_settings for *args*, or None."""
        spec = request.env["incubacloud.mcp.catalog"]._spec("update_settings")
        return {"text": request.env["incubacloud.mcp.tools"]._confirmation(spec, args)}


@tagged("post_install", "-at_install")
class TestSettingsTools(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """The MCP's settings routes, get_settings and update_settings."""

    def setUp(self):
        """A manager with an API key, the panel fixture and the settings record."""
        super().setUp()
        self.resource = self._resource_here()
        self._panel_fixture()
        self.manager = self._cloud_user("mcp-set-mgr", group="group_cloud_manager")
        self.key = self._api_key(self.manager)
        self.settings = self.env["cloud.settings"]._get_system()
        self.env.registry.clear_cache("routing")

    def _session_rpc(self, route, params=None):
        """Call a JSON-RPC route of the panel with the logged-in session."""
        body = {"jsonrpc": "2.0", "method": "call", "params": params or {}}
        return self.url_open(
            route,
            data=json.dumps(body),
            headers={"Content-Type": "application/json"},
        ).json()

    def test_the_connect_page_data_is_for_every_role(self):
        """A stakeholder reads what the Connect an agent page shows."""
        self._cloud_user("mcp-set-stakeholder", group="group_cloud_user")
        self.authenticate("mcp-set-stakeholder", "mcp-set-stakeholder")
        config = self._session_rpc("/cloud/mcp/config")["result"]
        self.assertEqual(config["resource"], self.resource)
        self.assertEqual(
            (config["issuer"], config["api_keys"], config["managed"]),
            ("", True, False),
        )
        self.assertTrue(config["docs_url"])

    def test_a_manager_saves_the_mcp_settings_and_a_managed_panel_refuses_the_issuer(
        self,
    ):
        """A manager saves the issuer and the limit; a managed panel keeps its own."""
        self.authenticate("mcp-set-mgr", "mcp-set-mgr")
        saved = self._session_rpc(
            "/cloud/mcp/save_settings",
            {
                "vals": {
                    "rate_limit_mcp_user_per_min": 30,
                    "mcp_oauth_issuer": "https://idp.example.test/",
                }
            },
        )
        self.assertTrue(saved["result"]["ok"])
        self.assertEqual(self.settings.rate_limit_mcp_user_per_min, 30)
        self.assertEqual(self.settings.mcp_oauth_issuer, "https://idp.example.test")
        with patch.object(
            type(self.env["cloud.settings"]),
            "_mcp_settings_managed",
            autospec=True,
            return_value=True,
        ):
            refused = self._session_rpc(
                "/cloud/mcp/save_settings",
                {"vals": {"mcp_oauth_issuer": "https://other.example.test"}},
            )
        self.assertFalse(refused["result"]["ok"])
        self.assertEqual(self.settings.mcp_oauth_issuer, "https://idp.example.test")

    def test_settings_are_a_manager_s(self):
        """A developer neither sees nor runs the settings tools."""
        key = self._api_key(self._cloud_user("mcp-set-dev"))
        self._assert_refused_by_role(key, "get_settings", {})
        self._assert_refused_by_role(
            key, "update_settings", {"section": "general", "values": {}}
        )

    def test_get_settings_has_every_section_and_no_secret(self):
        """get_settings answers every section, with no secret in it."""
        result = self._call_tool(
            self.key, "get_settings", {"backend_id": self.backend.id}
        )
        payload = self._payload(result)
        for section in (
            "general",
            "rate_limits",
            "mcp",
            "backup_backends",
            "github_app",
            "backup_backend",
        ):
            self.assertIn(section, payload)
        self.assertIn("rate_limit_mcp_user_per_min", payload["rate_limits"])
        self._assert_no_secret(json.dumps(result))

    def _general(self):
        """The general settings, as get_settings reads them."""
        return self._payload(self._call_tool(self.key, "get_settings"))["general"]

    def test_changing_one_general_field_keeps_the_others(self):
        """update_settings changes the field given and leaves every other one
        as it was, even one the route would not write back the same (a
        metrics retention of 0 it would save as 1)."""
        self.settings.write(
            {
                "job_retention_days": 200,
                "job_log_retention_days": 30,
                "metrics_retention_days": 0,
            }
        )
        before = self._general()
        result = self._call_tool(
            self.key,
            "update_settings",
            {"section": "general", "values": {"job_log_retention_days": 45}},
        )
        self.assertFalse(result["isError"], result)
        self.assertEqual(self._general(), before | {"job_log_retention_days": 45})
        self.assertEqual(self.settings.metrics_retention_days, 0)

    def test_changing_the_app_id_keeps_the_installation(self):
        """A GitHub App change keeps the fields not given; others are refused."""
        app = self.github_app
        app.write({"app_id": "111", "installation_id": "222"})
        result = self._call_tool(
            self.key,
            "update_settings",
            {"section": "github_app", "values": {"app_id": "333"}},
        )
        self.assertFalse(result["isError"], result)
        self.assertEqual((app.app_id, app.installation_id), ("333", "222"))
        refused = self._call_tool(
            self.key,
            "update_settings",
            {"section": "github_app", "values": {"force": True}},
        )
        self.assertTrue(refused["isError"])
        self.assertIn("force", refused["content"][0]["text"])

    def test_an_unknown_setting_is_refused(self):
        """A key the section does not have is refused, by name."""
        result = self._call_tool(
            self.key,
            "update_settings",
            {"section": "general", "values": {"job_retention_dayz": 1}},
        )
        self.assertTrue(result["isError"])
        self.assertIn("job_retention_dayz", result["content"][0]["text"])

    def test_a_value_equal_once_normalised_needs_no_approval(self):
        """A number given as text, or the issuer with its final slash, is no
        change: it needs no approval."""
        self.settings.write(
            {
                "mcp_oauth_issuer": "https://idp.example.test",
                "rate_limit_mcp_user_per_min": 30,
                "trusted_proxy_ranges": "10.0.0.0/8",
            }
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.audit_log_retention_days", "90"
        )
        for args in (
            {
                "section": "mcp",
                "values": {"mcp_oauth_issuer": "https://idp.example.test/"},
            },
            {"section": "rate_limits", "values": {"rate_limit_mcp_user_per_min": "30"}},
            {"section": "general", "values": {"audit_log_retention_days": "90"}},
            {"section": "general", "values": {"trusted_proxy_ranges": " 10.0.0.0/8\n"}},
        ):
            with self.subTest(args=args):
                result = self._call_tool(self.key, "update_settings", args)
                self.assertFalse(result["isError"], result)

    def _approval_text(self, section, values):
        """What update_settings asks the manager to approve, or None."""
        params = {"args": {"section": section, "values": values}}
        return self._session_rpc("/cloud/mcp_test/approval_text", params)["result"][
            "text"
        ]

    def test_the_approval_says_each_security_setting_from_what_to_what(self):
        """Retention, limits and the issuer each ask, with old and new values."""
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.audit_log_retention_days", "90"
        )
        self.settings.write(
            {
                "rate_limit_logs_per_min": 60,
                "mcp_oauth_issuer": "https://idp.example.test",
            }
        )
        self.authenticate("mcp-set-mgr", "mcp-set-mgr")
        cases = (
            (
                "general",
                {"audit_log_retention_days": 1},
                "audit_log_retention_days: 90 → 1",
            ),
            (
                "rate_limits",
                {"rate_limit_logs_per_min": 6000},
                "rate_limit_logs_per_min: 60 → 6000",
            ),
            (
                "mcp",
                {"mcp_oauth_issuer": "https://evil.example.test"},
                "mcp_oauth_issuer: 'https://idp.example.test' → 'https://evil.example.test'",
            ),
        )
        for section, values, line in cases:
            with self.subTest(section=section):
                text = self._approval_text(section, values)
                self.assertIn("security settings", text)
                self.assertIn(line, text)

    def test_observability_asks_only_where_the_panel_hides_it(self):
        """Metrics settings ask for approval on a panel that hides them only."""
        values = {
            "metrics_retention_days": (self.settings.metrics_retention_days or 0) + 7
        }
        self.authenticate("mcp-set-mgr", "mcp-set-mgr")
        self.assertIsNone(self._approval_text("general", values))
        Dispatch = type(self.env["incubacloud.mcp.dispatch"])
        original = Dispatch._call

        def hidden(dispatch, path, params):
            """Answer get_config as a tenant panel does: observability hidden."""
            result = original(dispatch, path, params)
            if path == "/cloud/get_config":
                result["features"]["observability"]["configure"] = False
            return result

        with patch.object(Dispatch, "_call", autospec=True, side_effect=hidden):
            text = self._approval_text("general", values)
        self.assertIn("metrics_retention_days", text)

    def test_a_sensitive_change_does_not_happen_without_approval(self):
        """Shortening the audit retention waits for an approval, and nothing changes."""
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.audit_log_retention_days", "90"
        )
        result = self._call_tool(
            self.key,
            "update_settings",
            {"section": "general", "values": {"audit_log_retention_days": 1}},
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("incubacloud.audit_log_retention_days"),
            "90",
        )
