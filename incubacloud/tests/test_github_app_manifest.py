"""The GitHub App a panel creates may comment on pull requests.

A pull request preview leaves a comment with its URL on the pull request
(``cloud.instance._post_or_update_pr_comment``), which GitHub only allows
an App with write access to pull requests. The manifest asked for read,
so every comment failed with a 403 that only reached the log (B6, found
writing QA's GitHub walks, 8-oct-2026).
"""
import base64
import json
import re

from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged("-at_install", "post_install")
class TestGitHubAppManifest(HttpCase):

    def setUp(self):
        """A manager of the panel, who may set GitHub up."""
        super().setUp()
        self.env["res.users"].create({
            "name": "manifest-manager",
            "login": "manifest-manager",
            "password": "manifest-manager",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_manager").id,
            ])],
        })

    def _manifest(self):
        """The manifest the setup page hands to GitHub."""
        self.authenticate("manifest-manager", "manifest-manager")
        response = self.url_open(
            "/cloud/github/setup", headers={"X-Forwarded-Proto": "https"}
        )
        self.assertEqual(response.status_code, 200)
        found = re.search(r'data-m="([^"]+)"', response.text)
        self.assertTrue(found, "the setup page carries no manifest")
        return json.loads(base64.b64decode(found.group(1)))

    def test_the_app_may_comment_on_pull_requests(self):
        permissions = self._manifest()["default_permissions"]
        self.assertEqual(permissions["pull_requests"], "write")
        self.assertEqual(permissions["contents"], "read")
