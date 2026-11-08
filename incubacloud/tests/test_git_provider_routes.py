"""The panel's routes for Git providers, and the reads that dispatch on them.

Settings → Git providers adds, edits, tests and deletes a GitLab server
and its credentials; the editor's reads (branches, head, modules,
requirements, freeze) go to that server with git when the line is
GitLab's, and to GitHub as before otherwise (plan-gitlab.md §4.5,
§4.11, 8-nov-2026).
"""
from unittest.mock import MagicMock, patch

from odoo import http as odoo_http
from odoo.exceptions import AccessError, UserError
from odoo.http import Request
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_crud,
    _routes_git_providers,
    _routes_github,
    _routes_ops,
)
from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController
from odoo.addons.incubacloud.gitlab.client import GitLabError
from odoo.addons.incubacloud.models import cloud_git_provider as provider_mod
from odoo.addons.incubacloud.net import git_reader
from odoo.addons.incubacloud.net.git_reader import GitReaderError

_URL = "https://gitlab.com/acme/addons"
_SHA = "f2807287adcb0123456789abcdef0123456789ab"


@tagged("post_install", "-at_install")
class _RoutesCase(TransactionCase):
    """A provider of gitlab.com and a way to call the routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["cloud.git.provider"].create({
            "name": "GitLab.com", "base_url": "https://gitlab.com",
        })
        cls.manager = new_test_user(
            cls.env, login="git-routes-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        cls.consultant = new_test_user(
            cls.env, login="git-routes-consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )

    def setUp(self):
        super().setUp()
        # The hourly cap of git reads is kept in a transaction of its own.
        patcher = patch.object(
            _routes_github, "_consume_github_quota", autospec=True,
            return_value=True,
        )
        self.quota = patcher.start()
        self.addCleanup(patcher.stop)

    def _serve(self, route, user=None, **kwargs):
        """Call *route* of the panel's controller as *user* would.

        :param str route: the controller method's name
        :param user: the user asking, the superuser when None
        :param kwargs: the route's arguments
        :return: what the route answers
        """
        env = self.env(user=user) if user else self.env
        controller = CloudDataLoadController()
        controller._sec = lambda: env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = env
        with patch.object(odoo_http, "request", fake_request), \
                patch.object(_routes_github, "request", fake_request), \
                patch.object(_routes_git_providers, "request", fake_request), \
                patch.object(_routes_crud, "request", fake_request), \
                patch.object(_routes_ops, "request", fake_request):
            return getattr(controller, route)(**kwargs)

    def _cred(self, namespace, kind="project", token="glpat-routes12", **vals):
        """Create a credential of the test provider.

        :rtype: cloud.git.credential
        """
        return self.env["cloud.git.credential"].create({
            "provider_id": self.provider.id, "namespace": namespace,
            "kind": kind, "token": token,
        } | vals)


@tagged("post_install", "-at_install")
class TestProviderRoutes(_RoutesCase):
    """get, save, delete and test a provider (§4.5)."""

    def test_a_consultant_sees_the_providers_but_no_credential(self):
        self._cred("acme", scopes="api")
        answer = self._serve("cloud_get_git_providers", user=self.consultant)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["network"], "any")
        self.assertFalse(answer["can_manage"])
        row = next(p for p in answer["providers"] if p["id"] == self.provider.id)
        self.assertEqual(row["base_url"], "https://gitlab.com")
        self.assertEqual(row["authority"], "gitlab.com")
        self.assertNotIn("credentials", row)

    def test_a_manager_sees_the_credentials_without_their_token(self):
        cred = self._cred("acme", kind="deploy", username="gitlab+deploy-token-7")
        answer = self._serve("cloud_get_git_providers", user=self.manager)
        self.assertTrue(answer["can_manage"])
        row = next(p for p in answer["providers"] if p["id"] == self.provider.id)
        self.assertEqual(row["credentials"], [{
            "id": cred.id, "namespace": "acme", "kind": "deploy",
            "username": "gitlab+deploy-token-7", "scopes": "",
            "token_user": "", "expires_on": False, "has_token": True,
        }])
        self.assertNotIn("glpat-routes12", repr(answer))

    def test_a_consultant_cannot_save_delete_or_test(self):
        for route, kwargs in (
            ("cloud_save_git_provider", {"vals": {"name": "x"}}),
            ("cloud_delete_git_provider", {"provider_id": self.provider.id}),
            ("cloud_test_git_provider", {
                "provider_id": self.provider.id, "url": _URL,
            }),
        ):
            with self.subTest(route=route), self.assertRaises(AccessError):
                self._serve(route, user=self.consultant, **kwargs)

    def test_saving_creates_the_provider_and_introspects_a_new_token(self):
        with patch.object(
            provider_mod.CloudGitCredential, "_introspect", autospec=True,
            return_value="api read_repository",
        ) as introspect:
            answer = self._serve("cloud_save_git_provider", user=self.manager, vals={
                "name": "Acme GitLab", "base_url": "https://Git.Acme.example/",
                "credentials": [
                    {"namespace": "Acme", "kind": "group", "token": "glpat-groupsave1"},
                    {"namespace": "acme/addons", "kind": "deploy",
                     "username": "gitlab+deploy-token-3", "token": "gldt-deploysave1"},
                ],
            })
        self.assertTrue(answer["ok"], answer)
        provider = self.env["cloud.git.provider"].browse(answer["provider"]["id"])
        self.assertEqual(provider.base_url, "https://git.acme.example")
        self.assertEqual(
            provider.credential_ids.mapped("namespace"), ["acme", "acme/addons"],
        )
        # A deploy token never answers on the API: only the group token is asked.
        self.assertEqual(introspect.call_count, 1)
        self.assertEqual(introspect.call_args.args[0].namespace, "acme")
        self.assertEqual(answer["warnings"], [])

    def test_an_empty_token_keeps_the_stored_one_and_asks_nothing(self):
        cred = self._cred("acme")
        with patch.object(
            provider_mod.CloudGitCredential, "_introspect", autospec=True,
        ) as introspect:
            answer = self._serve("cloud_save_git_provider", user=self.manager, vals={
                "id": self.provider.id, "name": "GitLab", "base_url": "https://gitlab.com",
                "credentials": [{"id": cred.id, "namespace": "acme", "kind": "project",
                                 "token": ""}],
            })
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(cred.sudo().token, "glpat-routes12")
        self.assertEqual(self.provider.name, "GitLab")
        introspect.assert_not_called()

    def test_a_credential_left_out_is_removed(self):
        keep = self._cred("acme")
        drop = self._cred("other")
        answer = self._serve("cloud_save_git_provider", user=self.manager, vals={
            "id": self.provider.id, "name": "GitLab.com", "base_url": "https://gitlab.com",
            "credentials": [{"id": keep.id, "namespace": "acme", "kind": "project"}],
        })
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(keep.exists())
        self.assertFalse(drop.exists())

    def test_an_introspection_that_fails_saves_and_warns(self):
        with patch.object(
            provider_mod.CloudGitCredential, "_introspect", autospec=True,
            side_effect=UserError("GitLab.com did not answer about the token (unreachable)."),
        ):
            answer = self._serve("cloud_save_git_provider", user=self.manager, vals={
                "id": self.provider.id, "name": "GitLab.com",
                "base_url": "https://gitlab.com",
                "credentials": [{"namespace": "acme", "kind": "personal",
                                 "token": "glpat-warnsave1"}],
            })
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(len(self.provider.credential_ids), 1)
        self.assertIn("did not answer", answer["warnings"][0])

    def test_a_refused_value_saves_nothing(self):
        answer = self._serve("cloud_save_git_provider", user=self.manager, vals={
            "name": "Bad", "base_url": "https://bad.example",
            "credentials": [{"namespace": "a", "kind": "project", "token": "tok@evil1"}],
        })
        self.assertFalse(answer["ok"])
        self.assertFalse(self.env["cloud.git.provider"].search(
            [("authority", "=", "bad.example")],
        ))

    def test_delete_refuses_while_lines_use_it_and_says_how_many(self):
        project = self.env["cloud.project"].create({"name": "Routes provider"})
        self.env["cloud.project.repo"].with_context(skip_apply_requirements=True).create(
            {"project_id": project.id, "url": _URL, "branch": "main"},
        )
        answer = self._serve(
            "cloud_delete_git_provider", user=self.manager, provider_id=self.provider.id,
        )
        self.assertFalse(answer["ok"])
        self.assertIn("1", answer["error"])
        self.assertTrue(self.provider.exists())
        project.repo_ids.unlink()
        answer = self._serve(
            "cloud_delete_git_provider", user=self.manager, provider_id=self.provider.id,
        )
        self.assertTrue(answer["ok"], answer)
        self.assertFalse(self.provider.exists())

    def _test(self, ls_remote=None, introspect="api read_repository", url=_URL):
        """Run «Test connection» with git and the API answering as given.

        :return: ``(answer, ls_remote mock)``
        """
        with patch.object(
            git_reader.GitReader, "ls_remote", autospec=True,
            **({"side_effect": ls_remote} if isinstance(ls_remote, Exception)
               else {"return_value": {"HEAD": _SHA}}),
        ) as ls, patch.object(
            provider_mod.CloudGitCredential, "_introspect", autospec=True,
            **({"side_effect": introspect} if isinstance(introspect, Exception)
               else {"return_value": introspect}),
        ):
            answer = self._serve(
                "cloud_test_git_provider", user=self.manager,
                provider_id=self.provider.id, url=url,
            )
        return answer, ls

    def test_testing_checks_git_and_the_api_and_keeps_the_result(self):
        self._cred("acme", kind="personal")
        answer, ls = self._test()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["git"]["code"], "ok")
        self.assertEqual(answer["api"]["code"], "ok")
        ref, cred = ls.call_args.args[1:3]
        self.assertEqual(ref.path, "acme/addons")
        self.assertEqual(cred, ("oauth2", "glpat-routes12"))
        self.assertEqual(self.provider.last_check_git, "ok")
        self.assertEqual(self.provider.last_check_api, "ok")
        self.assertTrue(self.provider.last_check_at)

    def test_testing_names_each_channel_failure_without_the_token(self):
        self._cred("acme", kind="personal")
        cause = GitLabError("auth", status=401)
        refused = UserError("GitLab.com did not answer about the token (auth).")
        refused.__cause__ = cause
        answer, _ls = self._test(
            ls_remote=GitReaderError("tls"), introspect=refused,
        )
        self.assertEqual(answer["git"]["code"], "tls")
        self.assertIn("certificate", answer["git"]["message"])
        self.assertEqual(answer["api"]["code"], "auth")
        self.assertNotIn("glpat-routes12", repr(answer))
        self.assertEqual(self.provider.last_check_git, "tls")

    def test_a_deploy_token_has_no_api_and_no_credential_reads_as_public(self):
        answer, ls = self._test()
        self.assertEqual(answer["api"]["code"], "no_credential")
        self.assertIsNone(ls.call_args.args[2])
        self._cred("acme", kind="deploy", username="gitlab+deploy-token-7")
        answer, _ls = self._test()
        self.assertEqual(answer["git"]["code"], "ok")
        self.assertEqual(answer["api"]["code"], "no_api")

    def test_testing_a_url_of_another_server_is_refused(self):
        answer, ls = self._test(url="https://gitlab.example.org/acme/addons")
        self.assertFalse(answer["ok"])
        ls.assert_not_called()

    def test_the_config_says_where_the_panel_connects(self):
        config = self._serve("cloud_get_config", user=self.consultant)
        self.assertEqual(config["features"]["git_providers"], {"network": "any"})


@tagged("post_install", "-at_install")
class TestReadsDispatchOnTheProvider(_RoutesCase):
    """The editor's reads of a GitLab line go through git (§4.5)."""

    def setUp(self):
        super().setUp()
        self._cred("acme", kind="deploy", username="gitlab+deploy-token-7")
        # GitHub is never asked about a GitLab repository.
        patcher = patch.object(
            _routes_github, "GitHubAnonymousClient", autospec=True,
        )
        self.github = patcher.start()
        self.addCleanup(patcher.stop)

    def test_branches_come_from_ls_remote(self):
        with patch.object(
            git_reader.GitReader, "ls_remote", autospec=True,
            return_value={"refs/heads/19.0": _SHA, "refs/heads/8.0": _SHA},
        ) as ls:
            answer = self._serve("cloud_get_repo_branches", url=_URL)
        self.assertEqual(answer, {
            "ok": True, "branches": ["19.0", "8.0"],
            "provider": "gitlab", "host": "gitlab.com",
        })
        ref, cred, patterns = ls.call_args.args[1:4]
        self.assertEqual(ref.path, "acme/addons")
        self.assertEqual(cred, ("gitlab+deploy-token-7", "glpat-routes12"))
        self.assertEqual(patterns, ["refs/heads/*"])
        self.github.assert_not_called()
        self.assertEqual(
            self.quota.call_args.args[2], "rate_limit_git_reads_per_hour",
        )

    def test_the_head_of_a_branch_and_a_missing_one(self):
        with patch.object(
            git_reader.GitReader, "ls_remote", autospec=True,
            return_value={"refs/heads/19.0": _SHA},
        ):
            answer = self._serve("cloud_get_branch_head", url=_URL, branch="19.0")
            self.assertEqual(answer, {"ok": True, "sha": _SHA})
        with patch.object(
            git_reader.GitReader, "ls_remote", autospec=True, return_value={},
        ):
            answer = self._serve("cloud_get_branch_head", url=_URL, branch="nope")
        self.assertFalse(answer["ok"])
        self.assertIn("nope", answer["error"])
        self.github.assert_not_called()

    def test_modules_and_requirements_come_from_the_tree(self):
        with patch.object(
            git_reader.GitReader, "tree", autospec=True,
            return_value=["mod_a/__manifest__.py", "README.md"],
        ), patch.object(
            git_reader.GitReader, "read_files", autospec=True,
            return_value={"requirements.txt": "requests\n"},
        ):
            modules = self._serve("cloud_get_repo_modules", url=_URL, branch="19.0")
            reqs = self._serve("cloud_get_repo_requirements", url=_URL, branch="19.0")
        self.assertEqual(modules, {"ok": True, "modules": ["mod_a"]})
        self.assertEqual(reqs, {"ok": True, "found": True, "content": "requests\n"})
        with patch.object(
            git_reader.GitReader, "read_files", autospec=True,
            return_value={"requirements.txt": None},
        ):
            reqs = self._serve("cloud_get_repo_requirements", url=_URL, branch="19.0")
        self.assertEqual(reqs, {"ok": True, "found": False, "content": ""})

    def test_a_failed_read_says_why_and_points_to_the_git_providers(self):
        with patch.object(
            git_reader.GitReader, "ls_remote", autospec=True,
            side_effect=GitReaderError("auth"),
        ):
            answer = self._serve("cloud_get_repo_branches", url=_URL)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["branches"], [])
        self.assertEqual(answer["settings_hint"], "git_providers")
        self.assertIn("Git providers", answer["error"])

    def test_a_host_without_provider_is_named(self):
        answer = self._serve(
            "cloud_get_repo_branches", url="https://git.acme.tld/acme/addons",
        )
        self.assertFalse(answer["ok"])
        self.assertIn("There is no Git provider for git.acme.tld", answer["error"])
        self.assertEqual(answer["settings_hint"], "git_providers")

    def test_the_hourly_cap_stops_the_read(self):
        self.quota.return_value = False
        with patch.object(git_reader.GitReader, "ls_remote", autospec=True) as ls:
            answer = self._serve("cloud_get_repo_branches", url=_URL)
        self.assertFalse(answer["ok"])
        ls.assert_not_called()

    def test_freezing_a_gitlab_line_pins_its_head(self):
        project = self.env["cloud.project"].create({"name": "Freeze GitLab"})
        line = self.env["cloud.project.repo"].with_context(
            skip_apply_requirements=True,
        ).create({"project_id": project.id, "url": _URL, "branch": "19.0"})
        with patch.object(
            git_reader.GitReader, "ls_remote", autospec=True,
            return_value={"refs/heads/19.0": _SHA},
        ):
            answer = self._serve(
                "cloud_freeze_repo", repo_id=line.id, model="cloud.project.repo",
            )
        self.assertEqual(answer, {"ok": True, "sha": _SHA})
        self.assertEqual(line.commit_sha, _SHA)


@tagged("post_install", "-at_install")
class TestImportFromHostWithProviders(_RoutesCase):
    """A host's repos.yaml: userinfo dropped, a host without provider
    skipped and named (§4.4-6)."""

    def _import(self, remotes, name):
        """Import a host instance whose repos.yaml has *remotes*.

        :param dict remotes: ``{alias: url}``
        :param str name: the project's name in its copier answers
        :return: what the route answers
        """
        host = self.env["cloud.host"].create({
            "name": f"git-routes-{name.lower().replace(' ', '-')}",
            "ip_address": "192.0.2.84", "user": "ubuntu",
            "wildcard_domain": "git-routes.example.com",
        })
        repos_yaml = "".join(
            f"{alias}:\n  remotes:\n    {alias}: '{url}'\n  target: {alias} main\n"
            for alias, url in remotes.items()
        )
        reads = [(f"project_name: {name}\n", ""), (repos_yaml, "")]
        reads += [("", "")] * 11
        with patch.object(
            _routes_ops.OpsMixin, "_ssh_run_multi", autospec=True,
            return_value=reads,
        ), patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts", autospec=True,
        ):
            return self._serve(
                "cloud_import_host_instance",
                host_id=host.id, path=f"/srv/{name.lower().replace(' ', '-')}",
            )

    def test_userinfo_is_dropped_and_a_host_without_provider_skipped(self):
        answer = self._import({
            "addons": "https://oauth2:glpat-hostimport1@gitlab.com/Acme/Addons.git",
            "web": "git@github.com:OCA/web.git",
            "private": "https://git.unknown.example/acme/private.git",
        }, "Host GitLab")
        self.assertTrue(answer["ok"], answer)
        project = self.env["cloud.project"].browse(answer["project_id"])
        self.assertEqual(project.repo_ids.mapped("url"), [
            "https://gitlab.com/Acme/Addons.git",
            "https://github.com/OCA/web.git",
        ])
        self.assertEqual(project.repo_ids[0].provider_id, self.provider)
        self.assertEqual(len(answer["skipped_repos"]), 1)
        self.assertIn("git.unknown.example", answer["skipped_repos"][0])
        self.assertNotIn("glpat-hostimport1", repr(answer))
