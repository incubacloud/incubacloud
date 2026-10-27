"""A backup leaves through a single-use link of 15 minutes, audited twice."""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import (
    FORM_CAPABILITIES,
    ConfirmMixin,
    McpHttpMixin,
    McpUsersMixin,
    PanelFixtureMixin,
)


@tagged("post_install", "-at_install")
class TestDownload(
    ConfirmMixin, PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """download_backup hands out a link of its own, once approved."""

    def setUp(self):
        """A developer's agent, and a finished download job with its file."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.dev = self._cloud_user("mcp-dl-dev")
        self.key = self._api_key(self.dev)
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "backup_download")], limit=1
        )
        self.job = (
            self.env["cloud.job"]
            .sudo()
            .create(
                {
                    "name": "download",
                    "instance_id": self.production.id,
                    "host_id": self.host.id,
                    "job_type_id": job_type.id,
                    "state": "done",
                }
            )
        )
        self.env["ir.attachment"].sudo().create(
            {
                "name": "mcp-acme-prod.zip",
                "raw": b"ZIP-CONTENT",
                "res_model": "cloud.job",
                "res_id": self.job.id,
            }
        )

    def _link(self):
        """Approve the link and return its path on the test server."""
        payload = self._payload(
            self._confirmed_call(self.key, "download_backup", {"job_id": self.job.id})
        )
        self.assertTrue(payload["single_use"])
        return payload["url"].split(self.base_url().split("://", 1)[1], 1)[1]

    def _get(self, path):
        """GET *path* as an agent does: no session, the database named."""
        return self.url_open(path, headers={"X-Odoo-Database": self.env.cr.dbname})

    def test_the_link_is_approved_downloads_once_and_is_audited(self):
        """The approval names the file; the link serves it once, uncached,
        and both its issue and its use are audited."""
        first = self._call_tool(
            self.key, "download_backup", {"job_id": self.job.id}, FORM_CAPABILITIES
        )
        self.assertIn(
            "mcp-acme-prod.zip", first["inputRequests"]["confirm"]["params"]["message"]
        )
        path = self._link()
        response = self._get(path)
        self.assertEqual(
            (response.status_code, response.content), (200, b"ZIP-CONTENT")
        )
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self._get(path).status_code, 404)
        used = self.env["cloud.audit.log"].search(
            [("job_id", "=", self.job.id), ("action", "=", "Agent: backup downloaded")]
        )
        self.assertEqual(used.user_id, self.dev)
        self.assertEqual(used.details, "mcp-acme-prod.zip")
        self.assertIn("(test-agent)", used.via_client)
        self.assertTrue(
            self.env["cloud.audit.log"].search(
                [("action", "=", "Agent: download_backup")]
            )
        )

    def test_another_user_s_session_gets_404_and_the_link_survives(self):
        """A browser signed in as another user gets nothing, and the link
        still works for the agent."""
        path = self._link()
        self._cloud_user("mcp-dl-other")
        self.authenticate("mcp-dl-other", "mcp-dl-other")
        self.assertEqual(self.url_open(path).status_code, 404)
        self.authenticate(None, None)
        self.assertEqual(self._get(path).status_code, 200)

    def test_a_head_does_not_use_it(self):
        """A HEAD, as link previews send, does not spend the link."""
        path = self._link()
        response = self.opener.head(
            self.base_url() + path,
            headers={"X-Odoo-Database": self.env.cr.dbname},
            timeout=12,
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self._get(path).status_code, 200)

    def test_after_fifteen_minutes_it_is_404(self):
        """Past its fifteen minutes the link is gone."""
        path = self._link()
        grant = (
            self.env["incubacloud.mcp.download"]
            .sudo()
            .search([("job_id", "=", self.job.id)])
        )
        grant.expires_at = fields.Datetime.now() - timedelta(minutes=1)
        self.env.flush_all()
        self.assertEqual(self._get(path).status_code, 404)

    def test_a_user_who_lost_the_role_cannot_use_it(self):
        """A link of a user who is no longer a developer serves nothing."""
        path = self._link()
        self.dev.group_ids = [(3, self.env.ref("incubacloud.group_cloud_developer").id)]
        self.env.flush_all()
        self.assertEqual(self._get(path).status_code, 404)

    def test_a_download_not_ready_yet_is_refused_without_asking(self):
        """A download whose job has not finished is refused, nobody asked."""
        self.job.sudo().state = "started"
        result = self._call_tool(
            self.key, "download_backup", {"job_id": self.job.id}, FORM_CAPABILITIES
        )
        self.assertTrue(result["isError"])
        self.assertIn("not ready", result["content"][0]["text"])

    def test_a_job_that_prepared_no_download_is_refused(self):
        """Only a download's job gives a link."""
        self.job.sudo().job_type_id = self.env["cloud.job.type"].search(
            [("code", "!=", "backup_download"), ("code", "!=", False)], limit=1
        )
        result = self._call_tool(
            self.key, "download_backup", {"job_id": self.job.id}, FORM_CAPABILITIES
        )
        self.assertTrue(result["isError"])
        self.assertIn("did not prepare a backup download", result["content"][0]["text"])
