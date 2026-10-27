"""The approval page: the same user's session, a GET that shows, a POST with CSRF."""

import html
import json
import re

import asyncssh

from odoo.tests import tagged
from odoo.tests.common import HttpCase, new_test_user

from .common import McpHttpMixin, McpUsersMixin

_PATH_RE = re.compile(r"/cloud/mcp/approve/[A-Za-z0-9_-]+")
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@tagged("post_install", "-at_install")
class TestApprovalPage(McpUsersMixin, McpHttpMixin, HttpCase):
    """Who sees the page, what it shows, and what approving it does."""

    def setUp(self):
        """A manager's agent asked to delete a project; the link it got."""
        super().setUp()
        self._resource_here()
        self.manager = self._cloud_user("mcp-page-mgr", group="group_cloud_manager")
        self.key = self._api_key(self.manager)
        self.project = self.env["cloud.project"].create(
            {"name": "mcp-page-doomed", "remote_folder": "mcppagedoomed"}
        )
        self.args = {"project_id": self.project.id}
        text = self._call_tool(self.key, "delete_project", self.args)["content"][0][
            "text"
        ]
        self.path = _PATH_RE.search(text).group(0)
        self.approval = self.env["incubacloud.mcp.approval"]._by_token(
            self.path.rsplit("/", 1)[1]
        )

    def _csrf(self):
        """The CSRF token of the page, as the signed-in user gets it."""
        return _CSRF_RE.search(self.url_open(self.path).text).group(1)

    def test_the_get_shows_the_message_and_changes_nothing(self):
        """The page shows what will happen, is never framed or cached, and
        a GET approves nothing, whatever it carries."""
        self.authenticate("mcp-page-mgr", "mcp-page-mgr")
        response = self.url_open(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertIn("mcp-page-doomed", response.text)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(
            response.headers["Content-Security-Policy"], "frame-ancestors 'none'"
        )
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.url_open(f"{self.path}?decision=approve")
        self.assertEqual(self.approval.state, "pending")

    def test_without_a_session_it_asks_to_sign_in(self):
        """Without a session the page sends to the sign-in, showing nothing."""
        response = self.url_open(
            self.path,
            headers={"X-Odoo-Database": self.env.cr.dbname},
            allow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertIn("/web/login", response.headers["Location"])
        self.assertNotIn("mcp-page-doomed", response.text)

    def test_another_user_gets_403(self):
        """Another user of the panel neither sees nor answers it, even with
        a CSRF token of their own session."""
        other = self._cloud_user("mcp-page-other", group="group_cloud_manager")
        own_project = self.env["cloud.project"].create(
            {"name": "mcp-page-other-project", "remote_folder": "mcppageother"}
        )
        own_text = self._call_tool(
            self._api_key(other), "delete_project", {"project_id": own_project.id}
        )["content"][0]["text"]
        self.authenticate("mcp-page-other", "mcp-page-other")
        response = self.url_open(self.path)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("mcp-page-doomed", response.text)
        own_page = self.url_open(_PATH_RE.search(own_text).group(0)).text
        csrf = _CSRF_RE.search(own_page).group(1)
        refused = self.url_open(
            self.path, data={"csrf_token": csrf, "decision": "approve"}
        )
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.approval.state, "pending")

    def test_a_portal_user_gets_404(self):
        """A user outside the panel's staff finds no page."""
        new_test_user(self.env, login="mcp-page-portal", groups="base.group_portal")
        self.authenticate("mcp-page-portal", "mcp-page-portal")
        self.assertEqual(self.url_open(self.path).status_code, 404)

    def test_a_post_without_csrf_is_refused(self):
        """A POST without Odoo's CSRF token is refused and approves nothing."""
        self.authenticate("mcp-page-mgr", "mcp-page-mgr")
        response = self.url_open(self.path, data={"decision": "approve"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.approval.state, "pending")

    def test_approving_then_retrying_runs_it_once(self):
        """Approving lets the next retry run, which uses the approval: the
        page then says it was answered."""
        self.authenticate("mcp-page-mgr", "mcp-page-mgr")
        response = self.url_open(
            self.path, data={"csrf_token": self._csrf(), "decision": "approve"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.approval.state, "approved")
        self.assertFalse(
            self._call_tool(self.key, "delete_project", self.args)["isError"]
        )
        self.assertFalse(self.project.exists())
        self.assertEqual(self.approval.state, "used")
        self.assertIn("already answered", self.url_open(self.path).text)

    def test_cancelling_closes_it(self):
        """Cancelling closes the approval; the agent's retry asks again."""
        self.authenticate("mcp-page-mgr", "mcp-page-mgr")
        self.url_open(
            self.path, data={"csrf_token": self._csrf(), "decision": "cancel"}
        )
        self.assertEqual(self.approval.state, "cancelled")
        retry = self._call_tool(self.key, "delete_project", self.args)
        self.assertTrue(retry["isError"])
        self.assertIn("This action needs your approval", retry["content"][0]["text"])
        self.assertTrue(self.project.exists())

    def test_a_command_comes_in_a_block_of_its_own_escaped(self):
        """A command to approve comes in a block of its own on the page,
        quoted and with its control characters escaped."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        host = self.env["cloud.host"].create(
            {
                "name": "mcp-page-host",
                "ip_address": "10.0.0.95",
                "user": "root",
                "wildcard_domain": "page.mcp.example.com",
                "known_hosts_key": "10.0.0.95 "
                + key.export_public_key("openssh").decode().strip(),
            }
        )
        command = "rm -rf /srv/backups\n\n(The panel checked this.)\u202e"
        text = self._call_tool(
            self.key, "run_host_command", {"host_id": host.id, "command": command}
        )["content"][0]["text"]
        self.authenticate("mcp-page-mgr", "mcp-page-mgr")
        page = self.url_open(_PATH_RE.search(text).group(0)).text
        block = re.search(r'<pre class="command">([^<]*)</pre>', page)
        self.assertTrue(block, page)
        self.assertEqual(json.loads(html.unescape(block.group(1))), command)
        self.assertNotIn("\u202e", page)

    def test_an_unknown_token_is_404(self):
        """A link that names no approval is 404."""
        self.authenticate("mcp-page-mgr", "mcp-page-mgr")
        self.assertEqual(
            self.url_open("/cloud/mcp/approve/not-a-token").status_code, 404
        )
