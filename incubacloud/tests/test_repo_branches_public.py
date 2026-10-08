"""A public repository lists its branches without GitHub credentials.

The import dialog asks ``/cloud/get_repo_branches`` for the branch list.
That route tried the GitHub App and the PAT only, so a panel with neither
(every new customer's) answered "No GitHub credentials configured ... to
access private repositories" for a public repository, while the import
itself reads public repositories anonymously. QA's import walk found it
on 8-oct-2026.
"""
import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.github.client import GitHubAnonymousClient, GitHubAPIError

_URL = "https://github.com/incubacloud/qa-addons"


@tagged("-at_install", "post_install")
class TestBranchesOfAPublicRepository(HttpCase):

    def setUp(self):
        super().setUp()
        # No credentials at all, as on a new customer's panel.
        self.env["cloud.github.app"].sudo().search([]).unlink()
        self.env["cloud.settings"].sudo()._get().github_pat = False
        self.env["res.users"].create({
            "name": "branches-consultant",
            "login": "branches-consultant",
            "password": "branches-consultant",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_consultant").id,
            ])],
        })
        self.authenticate("branches-consultant", "branches-consultant")

    def _branches(self):
        """POST ``/cloud/get_repo_branches`` for the test repository."""
        payload = {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"url": _URL},
        }
        return self.url_open(
            "/cloud/get_repo_branches",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()["result"]

    def test_a_public_repository_lists_its_branches_anonymously(self):
        with patch.object(
            GitHubAnonymousClient, "get",
            return_value=[{"name": "19.0"}, {"name": "qa-preview"}],
        ) as get:
            answer = self._branches()
        self.assertEqual(answer, {"ok": True, "branches": ["19.0", "qa-preview"]})
        self.assertIn("/repos/incubacloud/qa-addons/branches", get.call_args.args[0])

    def test_a_private_repository_still_asks_for_credentials(self):
        with patch.object(
            GitHubAnonymousClient, "get",
            side_effect=GitHubAPIError(404, "Not Found"),
        ):
            answer = self._branches()
        self.assertFalse(answer["ok"])
        self.assertIn("No GitHub credentials configured", answer["error"])
