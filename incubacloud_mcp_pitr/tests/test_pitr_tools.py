"""Continuous recovery through an agent: read it, switch it, restore with approval."""

import json
from datetime import datetime
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud_mcp.tests.common import (
    FORM_CAPABILITIES,
    ConfirmMixin,
    McpHttpMixin,
    McpUsersMixin,
    PanelFixtureMixin,
)

#: The window the fixture's production can be restored within, in UTC.
WINDOW = (datetime(2026, 10, 19, 0, 0), datetime(2026, 10, 21, 0, 0))


@tagged("post_install", "-at_install")
class TestPitrTools(
    ConfirmMixin, PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """The three tools of the bridge, called as an agent calls them."""

    def setUp(self):
        """A manager's API key, and the panel's server, project and instances."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.manager = self._cloud_user("mcp-pitr-mgr", group="group_cloud_manager")
        self.key = self._api_key(self.manager)
        self.Instance = type(self.env["cloud.instance"])

    def test_the_bridge_installs_with_both_modules(self):
        """The bridge is installed where the MCP and PITR are."""
        bridge = self.env["ir.module.module"].search(
            [("name", "=", "incubacloud_mcp_pitr")]
        )
        self.assertEqual(bridge.state, "installed")

    def test_the_state_is_read(self):
        """get_pitr answers what the Backups tab reads."""
        with patch.object(
            self.Instance,
            "_pitr_status_vals",
            autospec=True,
            return_value={"state": "ok", "objective": "5"},
        ):
            payload = self._payload(
                self._call_tool(
                    self.key, "get_pitr", {"instance_id": self.production.id}
                )
            )
        self.assertEqual(payload["state"], "ok")

    def test_switching_on_queues_a_job(self):
        """configure_pitr enables it with the objective it was given."""
        with patch.object(
            self.Instance, "action_pitr_enable", autospec=True, return_value=False
        ) as enable:
            result = self._call_tool(
                self.key,
                "configure_pitr",
                {
                    "instance_id": self.production.id,
                    "action": "enable",
                    "objective": "15",
                },
            )
        self.assertFalse(result["isError"], result)
        self.assertEqual(enable.call_args.args[1], "15")

    def test_a_developer_cannot_switch_it(self):
        """Switching and restoring are a manager's, as in the panel."""
        developer = self._api_key(
            self._cloud_user("mcp-pitr-dev", group="group_cloud_developer")
        )
        self._assert_refused_by_role(
            developer,
            "configure_pitr",
            {"instance_id": self.production.id, "action": "disable"},
        )
        self._assert_refused_by_role(
            developer,
            "pitr_restore",
            {"instance_id": self.production.id, "target": "2026-10-20 11:41:00"},
        )

    def test_a_restore_asks_and_types_the_name_for_the_user(self):
        """The approval names the instance and the moment; once approved,
        the restore runs with the instance's name typed for the user."""
        args = {"instance_id": self.production.id, "target": "2026-10-20 11:41:00"}
        with patch.object(
            self.Instance, "_pitr_window", autospec=True, return_value=WINDOW
        ):
            first = self._call_tool(self.key, "pitr_restore", args, FORM_CAPABILITIES)
            message = first["inputRequests"]["confirm"]["params"]["message"]
            self.assertIn('"mcp-acme-prod"', message)
            self.assertIn('"2026-10-20 11:41:00" (UTC)', message)
            with patch.object(
                self.Instance, "action_pitr_restore", autospec=True, return_value=False
            ) as restore:
                final = self._confirmed_call(self.key, "pitr_restore", args)
        self.assertFalse(final["isError"], final)
        self.assertEqual(restore.call_args.args[1], "2026-10-20 11:41:00")

    def test_a_moment_the_panel_would_refuse_is_refused_before_asking(self):
        """A moment outside the window, or not a moment, is refused at
        once: nobody is asked to approve a restore that cannot run."""
        with patch.object(
            self.Instance, "_pitr_window", autospec=True, return_value=WINDOW
        ):
            for target in ("2026-10-22 08:00:00", "yesterday"):
                with self.subTest(target=target):
                    result = self._call_tool(
                        self.key,
                        "pitr_restore",
                        {"instance_id": self.production.id, "target": target},
                        FORM_CAPABILITIES,
                    )
                    self.assertTrue(result.get("isError"), result)
                    self.assertNotIn("inputRequests", result)
        self.assertFalse(self._approvals())

    def test_a_restore_while_a_job_runs_is_refused_before_asking(self):
        """While a continuous recovery job is queued or running, the restore
        is refused at once, as the panel refuses it."""
        busy = UserError("A continuous recovery job is already running.")
        with (
            patch.object(
                self.Instance, "_pitr_window", autospec=True, return_value=WINDOW
            ),
            patch.object(
                self.Instance, "_pitr_refuse_busy", autospec=True, side_effect=busy
            ),
        ):
            result = self._call_tool(
                self.key,
                "pitr_restore",
                {"instance_id": self.production.id, "target": "2026-10-20 11:41:00"},
                FORM_CAPABILITIES,
            )
        self.assertTrue(result.get("isError"), result)
        self.assertNotIn("inputRequests", result)
        self.assertIn("already running", json.dumps(result))
        self.assertFalse(self._approvals())

    def _approvals(self):
        """Return this test's user's approvals of a restore."""
        return (
            self.env["incubacloud.mcp.approval"]
            .sudo()
            .search([("tool", "=", "pitr_restore"), ("user_id", "=", self.manager.id)])
        )
