"""Server tools: a manager's, and never with the server's credentials."""

import json

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import McpHttpMixin, McpUsersMixin

NEW_HOST = {
    "name": "mcp-new-host",
    "ip_address": "203.0.113.50",
    "user": "root",
    "wildcard_domain": "new.mcp.example.com",
}


@tagged("post_install", "-at_install")
class TestManagerTools(McpUsersMixin, McpHttpMixin, HttpCase):
    """manage_host creates and changes servers, without their credentials."""

    def setUp(self):
        """A manager with an API key."""
        super().setUp()
        self._resource_here()
        self.key = self._api_key(
            self._cloud_user("mcp-mgr", group="group_cloud_manager")
        )

    def test_a_server_is_created_and_changed_without_credentials(self):
        """A server is created without the agent's credentials, then renamed.

        The panel gives it a random password, as to a server created in
        its form with the field empty; the agent never sees it.
        """
        result = self._call_tool(self.key, "manage_host", {"vals": NEW_HOST})
        host = self.env["cloud.host"].browse(self._payload(result)["id"])
        self.assertEqual(host.name, "mcp-new-host")
        self.assertTrue(host.password)
        self.assertNotIn(host.password, json.dumps(result))
        changed = self._call_tool(
            self.key,
            "manage_host",
            {"host_id": host.id, "vals": {"name": "mcp-renamed-host"}},
        )
        self.assertFalse(changed["isError"], changed)
        self.assertEqual(host.name, "mcp-renamed-host")

    def test_a_server_password_is_refused(self):
        """A password among the server's fields is refused, and nothing is created."""
        result = self._call_tool(
            self.key, "manage_host", {"vals": NEW_HOST | {"password": "x"}}
        )
        self.assertTrue(result["isError"])
        self.assertFalse(self.env["cloud.host"].search([("name", "=", "mcp-new-host")]))

    def test_whitelist_needs_a_server(self):
        """setup_whitelist without host_id is refused."""
        self.assertTrue(
            self._call_tool(self.key, "manage_host", {"setup_whitelist": True})[
                "isError"
            ]
        )

    def test_a_consultant_does_not_manage_servers(self):
        """manage_host is a manager's tool."""
        consultant = self._cloud_user(
            "mcp-mgr-consultant", group="group_cloud_consultant"
        )
        result = self._call_tool(
            self._api_key(consultant), "manage_host", {"vals": NEW_HOST}
        )
        self.assertTrue(result["isError"])
