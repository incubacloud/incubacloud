"""An address that is not a repository is answered as such, quietly.

A customer pasted a branch folder from the browser
(``https://github.com/<owner>/<repo>/tree/staging/addons``) into the
repository editor. The branch list parsed it inside each credential
attempt, took the parse error for a missing GitHub App, answered "No
GitHub credentials configured", and logged the error with its traceback,
which raised a log alert on the customer's panel (12-oct-2026). The
routes that read a repository now answer with the form's own message,
before asking GitHub anything and without an ERROR in the log.
"""
import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.github.client import (
    GitHubAnonymousClient,
    GitHubAppClient,
    GitHubPATClient,
)

_FOLDER = "https://github.com/example/odoo-addons/tree/staging/addons"


@tagged("-at_install", "post_install")
class TestNotARepositoryAddress(HttpCase):

    def setUp(self):
        super().setUp()
        # A PAT, so every credential attempt would have been reached.
        self.env["cloud.settings"].sudo()._get().github_pat = "ghp_test"
        self.env["res.users"].create({
            "name": "repo-url-consultant",
            "login": "repo-url-consultant",
            "password": "repo-url-consultant",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_consultant").id,
            ])],
        })
        self.authenticate("repo-url-consultant", "repo-url-consultant")

    def _call(self, route, **params):
        """POST a JSON-RPC call to *route* and return its result."""
        payload = {
            "jsonrpc": "2.0", "method": "call", "id": 1, "params": params,
        }
        return self.url_open(
            route,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()["result"]

    def test_each_repository_route_says_the_address_is_not_one(self):
        routes = (
            ("/cloud/get_repo_branches", {}, {"branches": []}),
            ("/cloud/get_branch_head", {"branch": "staging"}, {}),
            ("/cloud/get_repo_modules", {"branch": "staging"}, {"modules": []}),
            (
                "/cloud/get_repo_requirements", {"branch": "staging"},
                {"content": "", "found": False},
            ),
        )
        for route, extra, shape in routes:
            with self.subTest(route=route):
                with patch.object(
                    GitHubAppClient, "get", autospec=True,
                ) as app, patch.object(
                    GitHubPATClient, "get", autospec=True,
                ) as pat, patch.object(
                    GitHubAnonymousClient, "get", autospec=True,
                ) as anonymous, self.assertNoLogs(
                    "odoo.addons.incubacloud", level="ERROR",
                ):
                    answer = self._call(route, url=_FOLDER, **extra)
                self.assertFalse(answer["ok"])
                self.assertIn("is not a recognised GitHub URL", answer["error"])
                self.assertIn(_FOLDER, answer["error"])
                self.assertNotIn("credentials", answer["error"])
                for key, value in shape.items():
                    self.assertEqual(answer[key], value)
                app.assert_not_called()
                pat.assert_not_called()
                anonymous.assert_not_called()
