"""Only a Project Manager or higher points a repository line elsewhere.

From 10-nov-2026. A Consultant still changes the branch and the
instance's or project's pip dependencies, and may send the URL a line
already has, as the forms do on every save.
"""
import base64
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from odoo import http as odoo_http
from odoo.exceptions import AccessError
from odoo.http import Request

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_crud,
    _routes_github,
)
from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController
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


class TestNewInstanceRepoLines(CloudSecurityBase):
    """A Consultant or a Project Manager creates an instance in a project
    with repositories: its lines come from the project's (10-nov-2026).

    Below Developer the user cannot create a repository line, so before
    this the copy of the project's lines refused the instance. Below
    Project Manager a line names only a repository of the project's; its
    branch and its modules are the user's.
    """

    def setUp(self):
        super().setUp()
        self.consultant = self._create_user(
            "new_inst_consultant", "group_cloud_consultant",
        )
        self.pm = self._create_user(
            "new_inst_pm", "group_cloud_project_manager",
        )
        self.developer = self._create_user(
            "new_inst_developer", "group_cloud_developer",
        )
        self.project = self._create_project(
            "New instance repos",
            members=[self.consultant, self.pm, self.developer],
        )
        self.env["cloud.project.repo"].with_context(
            skip_apply_requirements=True,
        ).create({
            "project_id": self.project.id,
            "url": _MINE,
            "branch": "main",
            "addons": "mod_a",
        })
        self.host = self.env["cloud.host"].create({
            "name": "new-inst-host", "ip_address": "192.0.2.91",
            "user": "ubuntu", "wildcard_domain": "new-inst.example.com",
        })
        patcher = patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts",
            autospec=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create(self, user, name, **vals):
        """Create a staging of the project as *user*, through the model.

        :rtype: cloud.instance
        """
        return self.env["cloud.instance"].with_user(user).with_context(
            skip_apply_requirements=True,
        ).create({
            "name": name, "project_id": self.project.id,
            "environment": "staging",
        } | vals)

    def _serve(self, user, **vals):
        """Call ``/cloud/create_instance`` as *user*, as the panel's form
        and the MCP's create_instance do.

        :return: what the route answers
        """
        env = self.env(user=user)
        controller = _routes_crud.CrudMixin()
        controller._sec = lambda: env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = env
        with patch.object(_routes_crud, "request", fake_request):
            return controller.cloud_create_instance(vals={
                "project_id": self.project.id, "environment": "staging",
                "host_id": self.host.id,
            } | vals)

    def test_a_consultant_and_a_project_manager_get_the_project_s_lines(self):
        """Through the model, with nothing sent for the lines."""
        for user in (self.consultant, self.pm):
            with self.subTest(user=user.login):
                inst = self._create(user, f"model-{user.id}")
                self.assertEqual(
                    inst.repo_ids.read(["url", "branch", "addons"]),
                    [{
                        "id": inst.repo_ids.id, "url": _MINE,
                        "branch": "main", "addons": "mod_a",
                    }],
                )

    def test_a_consultant_and_a_project_manager_create_it_from_the_form(self):
        """The form sends the project's lines, a branch changed.

        Below Project Manager the line takes the project's URL; a Project
        Manager's keeps the spelling sent, made canonical.
        """
        for user, url in ((self.consultant, _MINE), (self.pm, _MINE + ".git")):
            with self.subTest(user=user.login):
                answer = self._serve(user, name=f"form-{user.id}", repos=[{
                    "url": "git@github.com:example/mine.git",
                    "branch": "19.0", "addons": "mod_b",
                    "from_project": True,
                }])
                inst = self.env["cloud.instance"].browse(answer["id"])
                self.assertEqual(
                    (inst.repo_ids.url, inst.repo_ids.branch,
                     inst.repo_ids.addons),
                    (url, "19.0", "mod_b"),
                )

    def test_a_consultant_cannot_name_another_repository(self):
        """In the form or through the model, no instance is left."""
        with self.assertRaisesRegex(
            AccessError,
            "Only a Project Manager or higher can add the repository "
            "https://github.com/example/private-elsewhere, which is not one "
            "of the project's.",
        ), self.env.cr.savepoint():
            self._serve(
                self.consultant, name="refused-form",
                repos=[{"url": _OTHER, "branch": "main"}],
            )
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self._create(
                self.consultant, "refused-model",
                repo_ids=[(0, 0, {"url": _OTHER, "branch": "main"})],
            )
        self.assertFalse(self.env["cloud.instance"].search(
            [("name", "in", ("refused-form", "refused-model"))],
        ))

    def test_a_consultant_sends_lines_through_the_model(self):
        """What a saved line lets them change is taken; the rest is not."""
        inst = self._create(
            self.consultant, "model-lines",
            repo_ids=[(0, 0, {
                "url": _MINE, "branch": "19.0", "excludes": "mod_c",
                "repo_path": "other-group/private",
            })],
        )
        line = inst.repo_ids.filtered(lambda r: r.branch == "19.0")
        self.assertEqual(
            (line.url, line.excludes, line.repo_path),
            (_MINE, "mod_c", "example/mine"),
        )

    def test_lines_that_exist_are_not_moved_to_a_new_instance(self):
        """Linking another instance's line is refused below Developer."""
        other = self._create_instance(self.project)
        Instance = self.env["cloud.instance"].with_user(self.consultant)
        with self.assertRaisesRegex(AccessError, "cannot be moved"), \
                self.env.cr.savepoint():
            Instance.with_context(skip_project_repos=True).create({
                "name": "linked", "project_id": self.project.id,
                "environment": "staging",
                "repo_ids": [(4, other.repo_ids.id)],
            })
        self.assertEqual(other.repo_ids.instance_id, other)

    def test_a_project_manager_and_a_developer_name_another_repository(self):
        """They point lines at any repository, as on a saved line."""
        for user in (self.pm, self.developer):
            with self.subTest(user=user.login):
                answer = self._serve(
                    user, name=f"other-{user.id}",
                    repos=[{"url": _OTHER, "branch": "main"}],
                )
                inst = self.env["cloud.instance"].browse(answer["id"])
                self.assertEqual(inst.repo_ids.url, _OTHER)

    def test_a_developer_gets_the_project_s_lines(self):
        """As before."""
        inst = self._create(self.developer, "developer")
        self.assertEqual(inst.repo_ids.url, _MINE)

    def test_a_project_manager_imports_a_repository(self):
        """The import creates the project, its lines and its production."""
        inspected = {
            "ok": True,
            "repo_name": "pm-import",
            "repo_type": "simple",
            "repos_data": [{
                "url": _MINE, "branch": "19.0", "alias": "addons",
                "addons": "", "requirements": "",
            }],
            "copier": {},
            "pip_deps": "",
            "apt_deps": "",
            "odoo_conf": "",
            "odoo_version": "19.0",
            "submodules": [],
        }
        fake_request = MagicMock(spec=Request)
        fake_request.env = self.env(user=self.pm)
        with patch.object(_routes_github, "request", fake_request), \
                patch.object(odoo_http, "request", fake_request):
            result = CloudDataLoadController()._create_bounded_github_import(
                inspected,
            )
        self.assertTrue(result["ok"], result)
        inst = self.env["cloud.instance"].browse(result["instance_id"])
        self.assertEqual(inst.repo_ids.url, _MINE)


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

    def _inspect(self, user, route, url):
        """Call the import *route* as *user* for *url*, GitHub not reached.

        The inspector and the import's records are stood in for: what
        is checked is the GitHub clients the route hands the inspector.

        :param str route: ``cloud_fetch_odoojs_submodules`` (the preview)
            or ``cloud_import_project``
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
            for module in (_routes_github, odoo_http):
                stack.enter_context(
                    patch.object(module, "request", fake_request),
                )
            inspector = stack.enter_context(patch.object(
                _routes_github, "GitHubImportInspector", autospec=True,
            )).return_value
            # Set in ``__init__``, so the class's spec does not carry it.
            inspector.budget = None
            inspector.inspect_preview.return_value = {"ok": True}
            inspector.inspect_import.return_value = {"ok": True}
            stack.enter_context(patch.object(
                _routes_github.GitHubMixin, "_create_bounded_github_import",
                autospec=True, return_value={"ok": True},
            ))
            for name in (
                "GitHubAppClient", "GitHubPATClient", "GitHubAnonymousClient",
            ):
                clients[name] = stack.enter_context(
                    patch.object(_routes_github, name, autospec=True),
                )
            result = getattr(controller, route)(url, "main")
        return result, {name: mock.called for name, mock in clients.items()}

    def test_a_consultant_previews_another_repository_as_a_public_one(self):
        result, built = self._inspect(
            self.consultant, "cloud_fetch_odoojs_submodules", _OTHER,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(built, {
            "GitHubAppClient": False, "GitHubPATClient": False,
            "GitHubAnonymousClient": True,
        })

    def test_a_consultant_previews_a_line_s_repository_with_the_panel_s(self):
        result, built = self._inspect(
            self.consultant, "cloud_fetch_odoojs_submodules",
            "git@github.com:Example/mine.git",
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(built["GitHubAppClient"] and built["GitHubPATClient"])

    def test_a_project_manager_previews_any_repository_with_the_panel_s(self):
        result, built = self._inspect(
            self.pm, "cloud_fetch_odoojs_submodules", _OTHER,
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(built["GitHubAppClient"] and built["GitHubPATClient"])

    def test_a_consultant_cannot_import_and_nothing_is_read(self):
        """Importing creates a project: from Project Manager up."""
        for url in (_OTHER, _MINE):
            with self.subTest(url=url), self.assertRaisesRegex(
                AccessError, "group_cloud_project_manager",
            ):
                self._inspect(self.consultant, "cloud_import_project", url)

    def test_a_project_manager_imports_with_the_panel_s(self):
        result, built = self._inspect(self.pm, "cloud_import_project", _OTHER)
        self.assertTrue(result["ok"], result)
        self.assertTrue(built["GitHubAppClient"] and built["GitHubPATClient"])
