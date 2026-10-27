"""A backup leaves through a single-use link of 15 minutes, audited twice."""

import os
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import (
    ACCEPT,
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
        self.attachment = (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": "mcp-acme-prod.zip",
                    "raw": b"ZIP-CONTENT",
                    "res_model": "cloud.job",
                    "res_id": self.job.id,
                }
            )
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

    def test_an_approval_is_for_the_file_it_named(self):
        """A file the job gains after the approval is not the one approved:
        the retry hands out no link."""
        first = self._call_tool(
            self.key, "download_backup", {"job_id": self.job.id}, FORM_CAPABILITIES
        )
        self.env["ir.attachment"].sudo().create(
            {
                "name": "other.zip",
                "raw": b"OTHER",
                "res_model": "cloud.job",
                "res_id": self.job.id,
            }
        )
        final = self._call_tool(
            self.key,
            "download_backup",
            {"job_id": self.job.id},
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=ACCEPT,
        )
        self.assertTrue(final["isError"])
        self.assertFalse(
            self.env["incubacloud.mcp.download"]
            .sudo()
            .search([("job_id", "=", self.job.id)])
        )

    def test_a_file_gone_from_the_filestore_is_gone_not_a_crash(self):
        """A link whose file left the filestore answers 410, not an error."""
        path = self._link()
        full = self.attachment._full_path(self.attachment.store_fname)
        os.rename(full, full + ".moved")
        self.addCleanup(os.rename, full + ".moved", full)
        self.assertEqual(self._get(path).status_code, 410)

    def test_the_file_name_cannot_point_elsewhere(self):
        """The name the file is saved under is a plain name: no directories,
        quotes or control characters."""
        self.attachment.name = '../../etc/pa"ss\nwd.zip'
        disposition = self._get(self._link()).headers["Content-Disposition"]
        self.assertNotIn("/", disposition)
        self.assertNotIn("..", disposition)
        self.assertNotIn("\n", disposition)
        self.assertIn("passwd.zip", disposition)

    def test_a_link_is_issued_only_for_the_file_the_call_was_bound_to(self):
        """The handler checks the file again: a call bound to another one
        issues no link."""
        tools = self.env["incubacloud.mcp.tools"].with_user(self.dev)
        with self.assertRaises(UserError):
            tools._tool_download_backup(
                {"job_id": self.job.id, "attachment_id": self.attachment.id + 1}
            )
        self.assertFalse(
            self.env["incubacloud.mcp.download"]
            .sudo()
            .search([("job_id", "=", self.job.id)])
        )

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
