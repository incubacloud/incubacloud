"""Only a Project Manager or higher points a repository line elsewhere.

From 10-nov-2026. A Consultant still changes the branch and the
instance's or project's pip dependencies, and may send the URL a line
already has, as the forms do on every save.
"""
import base64
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.http import Request

from odoo.addons.incubacloud.controllers._data_load import _routes_github
from odoo.addons.incubacloud.github.client import GitHubAPIError
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
                with self.assertRaisesRegex(
                    AccessError,
                    "Only a Project Manager or higher can change the URL of "
                    "the repository https://github.com/example/mine; you can "
                    "still change its branch.",
                ):
                    self._as(line, self.consultant).write({
                        "url": _OTHER, "branch": "19.0", "addons": "",
                    })
                self.assertEqual((line.url, line.branch), (_MINE, "main"))

    def test_a_consultant_cannot_change_the_url_through_its_parent(self):
        """A write of the project or the instance that updates the line."""
        for line in self.lines:
            parent = line[line._owner_field]
            with self.subTest(model=line._name):
                with self.assertRaisesRegex(AccessError, "Project Manager"):
                    self._as(parent, self.consultant).write({
                        "repo_ids": [(1, line.id, {"url": _OTHER})],
                    })
                self.assertEqual(line.url, _MINE)

    def test_a_consultant_cannot_create_a_line(self):
        """Of a project or of an instance: the access rights say so."""
        for line in self.lines:
            with self.subTest(model=line._name), self.assertRaises(AccessError):
                self._as(self.env[line._name], self.consultant).create({
                    line._owner_field: line[line._owner_field].id,
                    "url": _OTHER,
                    "branch": "main",
                })

    def test_the_provider_and_path_follow_the_url_alone(self):
        """A value sent for them is dropped; the URL writes them."""
        provider = self.env["cloud.git.provider"].create({
            "name": "GitLab.com", "base_url": "https://gitlab.com",
        })
        for line in self.lines:
            with self.subTest(model=line._name):
                self._as(line, self.consultant).write({
                    "repo_path": "other-group/private",
                    "provider_id": provider.id,
                })
                self.assertEqual(
                    (line.repo_path, line.provider_id),
                    ("example/mine", self.env["cloud.git.provider"]),
                )
        line = self._as(self.env["cloud.project.repo"], self.pm).create({
            "project_id": self.project.id,
            "url": "https://github.com/example/another",
            "repo_path": "other-group/private",
            "provider_id": provider.id,
        })
        self.assertEqual(
            (line.repo_path, line.provider_id),
            ("example/another", self.env["cloud.git.provider"]),
        )

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


class TestRepoReadsByRole(CloudSecurityBase):
    """Below Project Manager the panel reads with its GitHub App or token
    only a repository of a line the user sees (10-nov-2026).

    Any other is read as a public repository: a public one still lists
    its branches, modules and requirements, a private one is not found.
    From Project Manager up, any repository, as the lines they write.
    """

    #: What each route's GitHub read answers, and the route's arguments.
    _ROUTES = (
        ("cloud_get_repo_branches", {}, [{"name": "main"}]),
        (
            "cloud_get_repo_modules", {"branch": "main"},
            {"tree": [{"path": "mod_a/__manifest__.py"}]},
        ),
        (
            "cloud_get_repo_requirements", {"branch": "main"},
            {"content": base64.b64encode(b"requests\n").decode()},
        ),
    )

    def setUp(self):
        super().setUp()
        self.consultant = self._create_user(
            "reads_consultant", "group_cloud_consultant",
        )
        self.pm = self._create_user("reads_pm", "group_cloud_project_manager")
        self.developer = self._create_user(
            "reads_developer", "group_cloud_developer",
        )
        self.project = self._create_project(
            "Repo reads", members=[self.consultant, self.pm, self.developer],
        )
        self.env["cloud.project.repo"].with_context(
            skip_apply_requirements=True,
        ).create({"project_id": self.project.id, "url": _MINE})
        Service = type(self.env["cloud.github.credential.service"])
        for name, value in (("get_credentials", {}), ("get_pat", "ghp_test")):
            patcher = patch.object(Service, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _read(self, user, route, url, answer, **kwargs):
        """Call *route* as *user* for *url*, GitHub answering *answer*.

        :return: the route's answer, and the clients it built, by name
        :rtype: tuple
        """
        env = self.env(user=user)
        controller = _routes_github.GitHubMixin()
        controller._sec = lambda: env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = env
        clients = {}
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(_routes_github, "request", fake_request),
            )
            for name in (
                "GitHubAppClient", "GitHubPATClient", "GitHubAnonymousClient",
            ):
                client = stack.enter_context(
                    patch.object(_routes_github, name, autospec=True),
                )
                if isinstance(answer, Exception):
                    client.return_value.get.side_effect = answer
                else:
                    client.return_value.get.return_value = answer
                clients[name] = client
            result = getattr(controller, route)(url=url, **kwargs)
        return result, {name: mock.called for name, mock in clients.items()}

    def test_a_consultant_reads_another_repository_as_a_public_one(self):
        for route, kwargs, answer in self._ROUTES:
            with self.subTest(route=route):
                result, built = self._read(
                    self.consultant, route, _OTHER, answer, **kwargs,
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(built, {
                    "GitHubAppClient": False, "GitHubPATClient": False,
                    "GitHubAnonymousClient": True,
                })

    def test_a_private_repository_is_not_found(self):
        result, _built = self._read(
            self.consultant, "cloud_get_repo_branches", _OTHER,
            GitHubAPIError(404, "Not Found"),
        )
        self.assertEqual(
            (result["ok"], result["error"]),
            (False, "The repository or branch was not found, or the "
             "credential cannot see it."),
        )

    def test_a_consultant_reads_a_line_s_repository_with_the_panel_s(self):
        """In any spelling of it."""
        for route, kwargs, answer in self._ROUTES:
            with self.subTest(route=route):
                result, built = self._read(
                    self.consultant, route,
                    "git@github.com:Example/mine.git", answer, **kwargs,
                )
                self.assertTrue(result["ok"], result)
                self.assertTrue(
                    built["GitHubAppClient"] or built["GitHubPATClient"],
                )

    def test_a_project_manager_and_a_developer_read_any_repository(self):
        for user in (self.pm, self.developer):
            for route, kwargs, answer in self._ROUTES:
                with self.subTest(user=user.login, route=route):
                    result, built = self._read(
                        user, route, _OTHER, answer, **kwargs,
                    )
                    self.assertTrue(result["ok"], result)
                    self.assertTrue(
                        built["GitHubAppClient"] or built["GitHubPATClient"],
                    )
