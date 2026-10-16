"""Pinning a repository to its branch head, whatever the panel's credentials.

A customer's panel with neither a GitHub App nor a PAT pressed the pin on
a public repository and read "Repo not found" (2026-10-15). Two faults:
saving a project deleted and recreated its repository rows, so the
editor's rows carried ids that no longer existed; and the branch-head
route tried the App and the PAT only, giving up with "Unexpected error"
where the branch list already read public repositories anonymously and,
failing that, said what to configure.
"""
import json
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_crud,
    _routes_github,
)
from odoo.addons.incubacloud.github.client import (
    GitHubAnonymousClient,
    GitHubAPIError,
)

REPO = "https://github.com/OCA/connector-ecommerce.git"
SHA = "060ad840f42817daf9f1377c0adb16f57e8831b4"


@tagged("-at_install", "post_install")
class TestPinningWithoutCredentials(HttpCase):
    """A panel with no GitHub App and no PAT."""

    def setUp(self):
        super().setUp()
        # Both credentials, not only the PAT: a database with an App of
        # its own — production's clone, which the deploy tests on — read
        # the repository through it and never reached the anonymous path.
        self.env["cloud.settings"].sudo()._get().github_pat = False
        self.env["cloud.github.app"].sudo().search([]).unlink()
        self.env["res.users"].create({
            "name": "pin-consultant",
            "login": "pin-consultant",
            "password": "pin-consultant",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_consultant").id,
            ])],
        })
        self.authenticate("pin-consultant", "pin-consultant")

    def _head(self, branch="12.0"):
        """Ask the branch head of REPO's *branch* as the consultant."""
        payload = {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"url": REPO, "branch": branch},
        }
        return self.url_open(
            "/cloud/get_branch_head",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()["result"]

    def test_a_public_repository_is_pinned_anonymously(self):
        with patch.object(
            GitHubAnonymousClient, "get", autospec=True,
            return_value={"commit": {"sha": SHA}},
        ) as anonymous:
            answer = self._head()
        self.assertEqual(answer, {"ok": True, "sha": SHA})
        self.assertIn("/branches/12.0", anonymous.call_args.args[1])

    def test_a_repository_github_does_not_show_says_what_to_configure(self):
        with patch.object(
            GitHubAnonymousClient, "get", autospec=True,
            side_effect=GitHubAPIError(404, "Not Found"),
        ):
            answer = self._head()
        self.assertFalse(answer["ok"])
        self.assertIn("No GitHub credentials configured", answer["error"])
        self.assertTrue(answer["settings_hint"])

    def test_a_branch_that_does_not_exist_is_named(self):
        def get(client, endpoint, **kwargs):
            if "/branches/" in endpoint:
                raise GitHubAPIError(404, "Branch not found")
            return {"full_name": "OCA/connector-ecommerce"}

        with patch.object(GitHubAnonymousClient, "get", autospec=True, side_effect=get):
            answer = self._head("99.0")
        self.assertFalse(answer["ok"])
        self.assertIn("Branch 99.0 does not exist", answer["error"])
        self.assertNotIn("settings_hint", answer)


class TestSavingAProjectKeepsItsRepositoryRows(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Pinned Project"})
        self.row = self.env["cloud.project.repo"].create({
            "project_id": self.project.id, "url": REPO, "branch": "main",
        })
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]

    def _save(self, repos):
        """Save the project with *repos* through the route."""
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, "request", fake):
            return self.controller.cloud_save_project(
                self.project.id, {"repos": repos},
            )

    def test_an_edited_row_keeps_its_id(self):
        answer = self._save([{"id": self.row.id, "url": REPO, "branch": "12.0"}])
        self.assertTrue(self.row.exists())
        self.assertEqual(self.row.branch, "12.0")
        self.assertEqual(answer["repos"], [{"id": self.row.id, "url": REPO}])

    def test_a_new_row_gets_an_id_and_a_removed_one_goes(self):
        other = "https://github.com/OCA/server-tools.git"
        answer = self._save([{"id": None, "url": other, "branch": "12.0"}])
        self.assertFalse(self.row.exists())
        (saved,) = answer["repos"]
        created = self.env["cloud.project.repo"].browse(saved["id"])
        self.assertEqual((created.url, created.project_id), (other, self.project))

    def test_pinning_an_edited_row_finds_it(self):
        self._save([{"id": self.row.id, "url": REPO, "branch": "12.0"}])
        fake = MagicMock(spec=Request)
        fake.env = self.env
        github = _routes_github.GitHubMixin()
        github._sec = lambda: self.env["cloud.security.mixin"]
        with patch.object(
            _routes_github, "request", fake,
        ), patch.object(
            _routes_github.GitHubMixin, "cloud_get_branch_head",
            return_value={"ok": True, "sha": SHA},
        ):
            answer = github.cloud_freeze_repo(self.row.id, "cloud.project.repo")
        self.assertEqual(answer, {"ok": True, "sha": SHA})
        self.assertEqual(self.row.commit_sha, SHA)
