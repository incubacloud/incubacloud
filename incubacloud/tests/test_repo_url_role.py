"""Only a Project Manager or higher points a repository line elsewhere.

The panel fetches a line's repository with its own GitHub App or token,
which may reach private repositories of other projects. A Consultant who
could rewrite a line's URL would have the panel fetch any of them into
an instance of theirs (10-nov-2026). A Consultant still changes the
branch and the instance's or project's pip dependencies, and may send the
URL a line already has, as the forms do on every save.
"""
from odoo.exceptions import AccessError

from odoo.addons.incubacloud.tests.test_cloud_security import (
    CloudSecurityBase,
)

_MINE = "https://github.com/example/mine"
_OTHER = "https://github.com/example/private-elsewhere"


class TestRepoUrlRole(CloudSecurityBase):

    def setUp(self):
        super().setUp()
        self.consultant = self._create_user(
            "repo_url_consultant", "group_cloud_consultant",
        )
        self.pm = self._create_user(
            "repo_url_pm", "group_cloud_project_manager",
        )
        self.project = self._create_project(
            "Repo URL", members=[self.consultant, self.pm],
        )
        self.instance = self._create_instance(self.project)
        self.instance_repo = self.env["cloud.instance.repo"].with_context(
            skip_apply_requirements=True,
        ).create({
            "instance_id": self.instance.id,
            "url": _MINE,
            "branch": "main",
        })
        self.project_repo = self.env["cloud.project.repo"].with_context(
            skip_apply_requirements=True,
        ).create({
            "project_id": self.project.id,
            "url": _MINE,
            "branch": "main",
        })
        self.lines = (self.instance_repo, self.project_repo)

    def _as(self, records, user):
        """*records* in *user*'s environment, without reading GitHub."""
        return records.with_user(user).with_context(
            skip_apply_requirements=True,
        )

    def test_a_consultant_cannot_change_the_url(self):
        """Of a project's line and of an instance's."""
        for line in self.lines:
            with self.subTest(model=line._name):
                with self.assertRaisesRegex(AccessError, "Project Manager"):
                    self._as(line, self.consultant).write({"url": _OTHER})
                self.assertEqual(line.url, _MINE)

    def test_a_consultant_cannot_change_the_url_with_the_branch(self):
        """Sent as a form sends it: every field of the line at once."""
        for line in self.lines:
            with self.subTest(model=line._name):
                with self.assertRaises(AccessError):
                    self._as(line, self.consultant).write({
                        "url": _OTHER, "branch": "19.0", "addons": "",
                    })
                self.assertEqual((line.url, line.branch), (_MINE, "main"))

    def test_a_consultant_changes_the_branch(self):
        """With the same URL sent along, and without it."""
        for line in self.lines:
            with self.subTest(model=line._name):
                self._as(line, self.consultant).write({
                    "url": _MINE, "branch": "19.0", "addons": "",
                    "excludes": "", "commit_sha": "",
                })
                self.assertEqual(line.branch, "19.0")
                self._as(line, self.consultant).write({"branch": "18.0"})
                self.assertEqual(line.branch, "18.0")

    def test_a_consultant_sends_the_url_in_another_spelling(self):
        """The same repository, in its canonical form."""
        for line in self.lines:
            with self.subTest(model=line._name):
                self._as(line, self.consultant).write(
                    {"url": "git@github.com:example/mine.git"},
                )
                self.assertEqual(line.url, _MINE + ".git")

    def test_a_consultant_changes_the_pip_dependencies(self):
        """Of the instance and of the project."""
        for owner in (self.instance, self.project):
            with self.subTest(model=owner._name):
                self._as(owner, self.consultant).write(
                    {"pip_dependencies": "requests==2.32.3"},
                )
                self.assertEqual(owner.pip_dependencies, "requests==2.32.3")

    def test_a_project_manager_changes_the_url(self):
        """Of a project's line and of an instance's."""
        for line in self.lines:
            with self.subTest(model=line._name):
                self._as(line, self.pm).write({"url": _OTHER})
                self.assertEqual(line.url, _OTHER)

    def test_the_system_changes_the_url(self):
        """With sudo, whoever the user is."""
        for line in self.lines:
            with self.subTest(model=line._name):
                self._as(line, self.consultant).sudo().write({"url": _OTHER})
                self.assertEqual(line.url, _OTHER)

    def test_the_permissions_tell_the_panel(self):
        """``can_change_repo_url`` from Project Manager up."""
        Security = self.env["cloud.security.mixin"]
        self.assertFalse(
            Security.with_user(self.consultant)._get_user_permissions()
            ["can_change_repo_url"],
        )
        self.assertTrue(
            Security.with_user(self.pm)._get_user_permissions()
            ["can_change_repo_url"],
        )
