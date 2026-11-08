"""A repository line names ``https://github.com/<owner>/<repo>`` and nothing else.

The build clones every repository line with the panel's GitHub token
(the App's or the PAT), and from 5-nov-2026 ``deploy.sh record-heads``
asks each one's head with it too. The token used to go to any URL that
contained ``github.com``, and the line's own check let through URLs with
a query, a fragment, a tab or a newline in them. Every way a line is
written (the panel's routes, which the MCP and the assistant call too,
the imports from GitHub and from a host) ends in the model, which now
stores a github.com repository in its canonical form and refuses the
rest (5-nov-2026).
"""
from unittest.mock import MagicMock, patch

from odoo import http as odoo_http
from odoo.exceptions import ValidationError
from odoo.http import Request
from odoo.tests.common import TransactionCase
from odoo.tools import SQL

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_crud,
    _routes_github,
    _routes_ops,
)
from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController
from odoo.addons.incubacloud.models import _repo_requirements
from odoo.addons.incubacloud.models._repo_requirements import (
    canonical_any_repo_url,
    canonical_repo_url,
    fetch_repo_addons,
    fetch_requirements_txt,
    is_safe_repo_url,
    resolve_repo_ref,
)
from odoo.addons.incubacloud.net import git_reader

#: Accepted by the old check, which matched ``[^/]+`` after the owner.
_ONCE_ACCEPTED = (
    "https://github.com/acme/addons?x=1",
    "https://github.com/acme/addons#main",
    "https://github.com/acme/addons\tpin\tdeadbeef",
    "https://github.com/acme/addons\nevil",
    "https://github.com/acme/addons@evil.example",
)
#: Never accepted, and still not.
_FOREIGN = (
    "https://evil.example/github.com/x.git",
    "https://github.com.evil.example/acme/addons",
    "https://user@github.com/acme/addons",
    "https://github.com:8443/acme/addons",
    "ftp://github.com/acme/addons",
)
#: A once-accepted URL every route below is sent.
_BAD = _ONCE_ACCEPTED[0]


class TestRepoUrlHost(TransactionCase):
    """Both repository models, and every way into them."""

    def setUp(self):
        """A project and a staging instance of it, with no repository."""
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Repo URL host"})
        self.instance = self.env["cloud.instance"].create({
            "name": "repo-url-host",
            "project_id": self.project.id,
            "environment": "staging",
            "state": "draft",
        })
        self.models = (
            ("cloud.project.repo", "project_id", self.project),
            ("cloud.instance.repo", "instance_id", self.instance),
        )
        # Nothing here asks GitHub: not the requirements, not the modules.
        for patcher in (
            patch.object(
                type(self.env["cloud.instance"]), "_check_addon_conflicts",
                autospec=True,
            ),
            patch.object(_routes_crud, "fetch_requirements_txt", return_value=""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _new(self, model, field, owner, url):
        """Create a line of *model* for *owner* with *url*, GitHub unasked."""
        return self.env[model].with_context(skip_apply_requirements=True).create(
            {field: owner.id, "url": url, "branch": "main"},
        )

    def _serve(self, module, controller, route, **kwargs):
        """Call *route* of *controller* as the panel's request would.

        :param module: the routes module whose ``request`` is patched
        :param controller: the controller instance
        :param str route: the method's name
        :param kwargs: the route's arguments
        :return: what it answers
        """
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = self.env
        with patch.object(module, "request", fake_request):
            return getattr(controller, route)(**kwargs)

    def test_a_url_that_is_not_the_canonical_one_is_refused(self):
        for model, field, owner in self.models:
            for url in _ONCE_ACCEPTED + _FOREIGN:
                with self.subTest(model=model, url=url), self.assertRaises(
                    ValidationError,
                ), self.env.cr.savepoint():
                    self._new(model, field, owner, url)

    def test_a_url_written_over_a_line_is_refused(self):
        for model, field, owner in self.models:
            line = self._new(model, field, owner, "https://github.com/acme/addons")
            for url in _ONCE_ACCEPTED + _FOREIGN:
                with self.subTest(model=model, url=url), self.assertRaises(
                    ValidationError,
                ), self.env.cr.savepoint():
                    line.write({"url": url})

    def test_every_spelling_of_a_github_repository_is_stored_canonical(self):
        for model, field, owner in self.models:
            for url, stored in (
                ("git@github.com:Acme/addons.git", "https://github.com/Acme/addons.git"),
                ("http://github.com/acme/addons", "https://github.com/acme/addons"),
                ("www.github.com/acme/addons/", "https://github.com/acme/addons"),
                ("  HTTPS://GitHub.com/acme/addons.git ", "https://github.com/acme/addons.git"),
                ("https://github.com/acme/addons", "https://github.com/acme/addons"),
            ):
                with self.subTest(model=model, url=url):
                    line = self._new(model, field, owner, url)
                    self.assertEqual(line.url, stored)
                    line.write({"url": url})
                    self.assertEqual(line.url, stored)

    def test_the_project_routes_refuse_it(self):
        """``/cloud/create_project`` and ``/cloud/save_project``: the
        panel's forms, the MCP's create_project, update_project and
        project_connect_repo, and the assistant's."""
        controller = _routes_crud.CrudMixin()
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._serve(
                _routes_crud, controller, "cloud_create_project",
                vals={"name": "Refused", "repos": [{"url": _BAD}]},
            )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._serve(
                _routes_crud, controller, "cloud_save_project",
                project_id=self.project.id, vals={"repos": [{"url": _BAD}]},
            )
        self.assertFalse(self.project.repo_ids)

    def test_the_instance_routes_refuse_it(self):
        """``/cloud/create_instance`` and ``/cloud/save_instance``: the
        panel's forms and the MCP's create_instance and update_instance."""
        host = self.env["cloud.host"].create({
            "name": "repo-url-host", "ip_address": "192.0.2.81", "user": "ubuntu",
            "wildcard_domain": "repo-url.example.com",
        })
        controller = _routes_crud.CrudMixin()
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._serve(
                _routes_crud, controller, "cloud_create_instance",
                vals={
                    "name": "refused", "project_id": self.project.id,
                    "environment": "staging", "host_id": host.id,
                    "repos": [{"url": _BAD}],
                },
            )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._serve(
                _routes_crud, controller, "cloud_save_instance",
                instance_id=self.instance.id, vals={"repos": [{"url": _BAD}]},
            )
        self.assertFalse(self.instance.repo_ids)

    def test_an_import_from_github_refuses_it(self):
        """A repository's own repos.yaml names the lines a GitHub import creates."""
        inspected = {
            "ok": True,
            "repo_name": "refused-import",
            "repo_type": "simple",
            "repos_data": [{
                "url": _BAD, "branch": "19.0", "alias": "addons",
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
        fake_request.env = self.env
        # ``odoo.http.request`` too: the import's messages are translated.
        with patch.object(_routes_github, "request", fake_request), \
                patch.object(odoo_http, "request", fake_request), \
                self.assertRaises(ValidationError), self.env.cr.savepoint():
            CloudDataLoadController()._create_bounded_github_import(inspected)
        self.assertFalse(
            self.env["cloud.project"].search([("name", "=", "refused-import")]),
        )

    def test_an_import_from_a_host_refuses_it(self):
        """A host's repos.yaml names the lines its import creates."""
        host = self.env["cloud.host"].create({
            "name": "repo-url-import", "ip_address": "192.0.2.82", "user": "ubuntu",
            "wildcard_domain": "repo-url-import.example.com",
        })
        repos_yaml = (
            "addons:\n"
            f"  remotes:\n    addons: '{_BAD}'\n"
            "  target: addons main\n"
            "  merges:\n  - addons main\n"
        )
        # The route's 13 reads: the copier answers, repos.yaml, the rest empty.
        # Answers that name the series, as a Doodba project's do: one that
        # names none is refused before its repository lines are made.
        reads = [
            ("odoo_version: 19.0\nproject_name: Refused host import\n", ""),
            (repos_yaml, ""),
        ]
        reads += [("", "")] * 11
        with patch.object(
            _routes_ops.OpsMixin, "_ssh_run_multi", autospec=True,
            return_value=reads,
        ), self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._serve(
                _routes_ops, _routes_ops.OpsMixin(), "cloud_import_host_instance",
                host_id=host.id, path="/srv/refused",
            )
        self.assertFalse(
            self.env["cloud.project"].search([("name", "=", "Refused host import")]),
        )

    def _store_as_before(self, line, url):
        """Give *line* the *url* a row kept from before 5-nov-2026.

        Written in SQL, as such a row was stored: neither ``write``'s
        rewrite nor the constraint see it.

        :param line: a repository line
        :param str url: the URL the row keeps
        """
        self.env.cr.execute(SQL(
            "UPDATE %s SET url = %s WHERE id = %s",
            SQL.identifier(line._table), url, line.id,
        ))
        line.invalidate_recordset(["url"])

    def test_an_old_spelling_is_stored_canonical_when_its_pin_or_branch_is_written(self):
        """Pinning, unpinning, a pull request's branch and the SaaS core
        pin write ``commit_sha`` or ``branch`` alone, and the constraint
        checks the URL then: a row's old spelling of a github.com
        repository is stored canonical, and the write goes through."""
        for model, field, owner in self.models:
            for url, stored in (
                ("github.com/acme/addons", "https://github.com/acme/addons"),
                ("http://github.com/acme/addons", "https://github.com/acme/addons"),
                ("https://www.github.com/acme/addons", "https://github.com/acme/addons"),
                ("https://github.com/acme/addons/", "https://github.com/acme/addons"),
                ("git@github.com:acme/addons.git", "https://github.com/acme/addons.git"),
            ):
                with self.subTest(model=model, url=url):
                    line = self._new(model, field, owner, "https://github.com/acme/addons")
                    self._store_as_before(line, url)
                    line.write({"commit_sha": "a" * 40})
                    self.assertEqual((line.url, line.commit_sha), (stored, "a" * 40))
                    self._store_as_before(line, url)
                    line.write({"branch": "feature/x", "commit_sha": False})
                    self.assertEqual((line.url, line.branch), (stored, "feature/x"))
                    self.assertFalse(line.commit_sha)

    def test_a_batch_write_stores_each_old_spelling_canonical(self):
        """``cloud_freeze_all_repos`` writes every line of an instance at once."""
        for model, field, owner in self.models:
            with self.subTest(model=model):
                old = self._new(model, field, owner, "https://github.com/acme/old")
                kept = self._new(model, field, owner, "https://github.com/acme/kept.git")
                self._store_as_before(old, "git@github.com:acme/old.git")
                (old | kept).write({"commit_sha": "b" * 40})
                self.assertEqual(
                    (old | kept).mapped("url"),
                    ["https://github.com/acme/old.git", "https://github.com/acme/kept.git"],
                )
                self.assertEqual((old | kept).mapped("commit_sha"), ["b" * 40] * 2)

    def test_an_old_url_that_cannot_be_canonical_still_fails_clearly(self):
        """No canonical github.com URL for it: the write is refused, naming
        the URL and the form it should take."""
        for model, field, owner in self.models:
            line = self._new(model, field, owner, "https://github.com/acme/addons")
            self._store_as_before(line, "https://evil.example/github.com/x.git")
            for vals in ({"commit_sha": "c" * 40}, {"branch": "dev"}):
                with self.subTest(model=model, vals=vals), self.assertRaisesRegex(
                    ValidationError,
                    r"There is no Git provider for evil\.example: add it "
                    r"under Settings → Git providers",
                ), self.env.cr.savepoint():
                    line.write(vals)
            self.assertEqual(line.url, "https://evil.example/github.com/x.git")

    def test_a_host_that_only_folds_to_github_in_unicode_is_not_github(self):
        """``gıthub.com`` (dotless i) and ``GİTHUB.com`` (dotted capital I)
        match ``github.com`` only under Unicode case folding: they are left
        as they are, and refused."""
        for url in ("https://gıthub.com/acme/addons", "https://GİTHUB.com/acme/addons"):
            with self.subTest(url=url):
                self.assertEqual(canonical_repo_url(url), url)
                self.assertFalse(is_safe_repo_url(canonical_repo_url(url)))
        for model, field, owner in self.models:
            with self.subTest(model=model), self.assertRaises(
                ValidationError,
            ), self.env.cr.savepoint():
                self._new(model, field, owner, "https://gıthub.com/acme/addons")

    def _import_from_host(self, remote, name):
        """Import a host's project whose repos.yaml names *remote*.

        :param str remote: the repository's remote in repos.yaml
        :param str name: the project's name in its copier answers
        :return: what the route answers
        """
        host = self.env["cloud.host"].create({
            "name": f"repo-url-{name.lower().replace(' ', '-')}",
            "ip_address": "192.0.2.83", "user": "ubuntu",
            "wildcard_domain": "repo-url-ssh.example.com",
        })
        repos_yaml = (
            "addons:\n"
            f"  remotes:\n    addons: '{remote}'\n"
            "  target: addons main\n"
        )
        reads = [(f"odoo_version: 19.0\nproject_name: {name}\n", ""), (repos_yaml, "")]
        reads += [("", "")] * 11
        with patch.object(
            _routes_ops.OpsMixin, "_ssh_run_multi", autospec=True,
            return_value=reads,
        ):
            return self._serve(
                _routes_ops, _routes_ops.OpsMixin(), "cloud_import_host_instance",
                host_id=host.id, path=f"/srv/{name.lower().replace(' ', '-')}",
            )

    def test_a_host_import_keeps_a_github_ssh_remote_and_skips_another_host(self):
        """The import used to turn any ``git@<host>:x/y.git`` into
        ``https://github.com/x/y.git``: a GitLab remote became a GitHub
        repository of the same path, cloned with the panel's token. A
        remote of a host with no Git provider is now left out and named,
        rather than failing the whole import (plan-gitlab.md §4.4-6)."""
        answer = self._import_from_host("git@github.com:acme/addons.git", "SSH github")
        self.assertTrue(answer["ok"], answer)
        project = self.env["cloud.project"].browse(answer["project_id"])
        self.assertEqual(project.repo_ids.mapped("url"), ["https://github.com/acme/addons.git"])
        answer = self._import_from_host("git@gitlab.com:acme/addons.git", "SSH gitlab")
        self.assertTrue(answer["ok"], answer)
        project = self.env["cloud.project"].browse(answer["project_id"])
        self.assertFalse(project.repo_ids)
        self.assertIn("gitlab.com", answer["skipped_repos"][0])


class TestRepoLineProvider(TransactionCase):
    """A line of a configured GitLab is stored canonical, with its provider
    and path (plan-gitlab.md §2.5, §4.1, 8-nov-2026)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["cloud.git.provider"].create({
            "name": "GitLab.com", "base_url": "https://gitlab.com",
        })
        cls.project = cls.env["cloud.project"].create({"name": "Repo provider"})
        cls.instance = cls.env["cloud.instance"].create({
            "name": "repo-provider", "project_id": cls.project.id,
            "environment": "staging", "state": "draft",
        })
        cls.models = (
            ("cloud.project.repo", "project_id", cls.project),
            ("cloud.instance.repo", "instance_id", cls.instance),
        )

    def setUp(self):
        super().setUp()
        patcher = patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts",
            autospec=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _new(self, model, field, owner, url):
        """Create a line of *model* for *owner* with *url*, nothing asked.

        :rtype: the line
        """
        return self.env[model].with_context(skip_apply_requirements=True).create(
            {field: owner.id, "url": url, "branch": "main"},
        )

    def test_a_gitlab_line_is_stored_canonical_with_its_provider_and_path(self):
        for model, field, owner in self.models:
            for url in (
                "https://gitlab.com/QA-Group/Sub/Addons",
                "gitlab.com/QA-Group/Sub/Addons.git",
                "git@gitlab.com:QA-Group/Sub/Addons.git",
            ):
                with self.subTest(model=model, url=url):
                    line = self._new(model, field, owner, url)
                    self.assertEqual(
                        line.url, "https://gitlab.com/QA-Group/Sub/Addons.git",
                    )
                    self.assertEqual(line.provider_id, self.provider)
                    self.assertEqual(line.repo_path, "qa-group/sub/addons")

    def test_a_github_line_has_no_provider_and_its_owner_and_repo(self):
        for model, field, owner in self.models:
            with self.subTest(model=model):
                line = self._new(model, field, owner, "git@github.com:Acme/Addons.git")
                self.assertFalse(line.provider_id)
                self.assertEqual(line.repo_path, "acme/addons")
                line.write({"url": "https://gitlab.com/acme/other"})
                self.assertEqual(line.provider_id, self.provider)
                self.assertEqual(line.repo_path, "acme/other")

    def test_a_host_with_no_provider_is_refused_naming_it(self):
        for model, field, owner in self.models:
            for url, host in (
                ("https://git.acme.tld/acme/addons", "git.acme.tld"),
                ("https://gitlab.com.evil.example/x/y", "gitlab.com.evil.example"),
            ):
                with self.subTest(model=model, url=url), self.assertRaisesRegex(
                    ValidationError,
                    f"There is no Git provider for {host}: add it under "
                    "Settings → Git providers",
                ), self.env.cr.savepoint():
                    self._new(model, field, owner, url)

    def test_capabilities_say_what_each_provider_offers_in_this_version(self):
        line = self._new(
            "cloud.project.repo", "project_id", self.project,
            "https://gitlab.com/acme/addons",
        )
        caps = line.repo_capabilities
        self.assertEqual(caps["provider"], "gitlab")
        self.assertEqual(caps["host"], "gitlab.com")
        self.assertTrue(caps["read"])
        self.assertEqual(caps["push_rebuild"], "unsupported")
        self.assertEqual(caps["previews"], "unsupported")
        self.assertEqual(caps["code_tools"], {"read": True, "write": False})
        github = self._new(
            "cloud.project.repo", "project_id", self.project,
            "https://github.com/acme/addons",
        )
        self.assertEqual(github.repo_capabilities["provider"], "github")
        self.assertEqual(github.repo_capabilities["host"], "github.com")
        self.assertIn(
            github.repo_capabilities["push_rebuild"], ("ok", "no_hook"),
        )

    def test_the_requirements_read_time_is_not_copied(self):
        field = self.env["cloud.instance.repo"]._fields["requirements_read_at"]
        self.assertEqual(field.type, "datetime")
        self.assertFalse(field.copy)

    def test_resolve_repo_ref_names_the_provider_or_nothing(self):
        ref = resolve_repo_ref(self.env, "gitlab.com/Acme/Addons")
        self.assertEqual(ref.kind, "gitlab")
        self.assertEqual(ref.path, "acme/addons")
        self.assertEqual(ref.canonical, "https://gitlab.com/Acme/Addons.git")
        gh = resolve_repo_ref(self.env, "https://github.com/Acme/Addons")
        self.assertEqual(gh.kind, "github")
        self.assertEqual(gh.path, "acme/addons")
        self.assertIsNone(resolve_repo_ref(self.env, "https://git.acme.tld/a/b"))
        self.assertEqual(
            canonical_any_repo_url(self.env, " gitlab.com/Acme/Addons "),
            "https://gitlab.com/Acme/Addons.git",
        )
        self.assertEqual(
            canonical_any_repo_url(self.env, "git@github.com:acme/addons"),
            "https://github.com/acme/addons",
        )
        self.assertEqual(
            canonical_any_repo_url(self.env, " https://git.acme.tld/a/b "),
            "https://git.acme.tld/a/b",
        )

    def test_the_reads_of_a_gitlab_line_go_through_git_with_its_credential(self):
        self.env["cloud.git.credential"].create({
            "provider_id": self.provider.id, "namespace": "acme",
            "kind": "deploy", "username": "gitlab+deploy-token-7",
            "token": "gldt-abcdefgh12",
        })
        url = "https://gitlab.com/acme/addons.git"
        with patch.object(
            git_reader.GitReader, "read_files", autospec=True,
            return_value={"requirements.txt": "requests\n"},
        ) as read, patch.object(
            git_reader.GitReader, "tree", autospec=True,
            return_value=[
                "mod_a/__manifest__.py", "README.md", "x/mod_b/__manifest__.py",
            ],
        ) as tree:
            content = fetch_requirements_txt(self.env, url, "19.0")
            addons = fetch_repo_addons(self.env, url, "19.0")
        self.assertEqual(content, "requests\n")
        self.assertEqual(addons, ["mod_a", "mod_b"])
        ref, cred, rev = read.call_args.args[1:4]
        self.assertEqual(ref.path, "acme/addons")
        self.assertEqual(cred, ("gitlab+deploy-token-7", "gldt-abcdefgh12"))
        self.assertEqual(rev, "19.0")
        self.assertEqual(tree.call_args.args[2], cred)

    def test_a_gitlab_read_that_fails_is_none_and_never_asks_github(self):
        url = "https://gitlab.com/acme/public.git"
        with patch.object(
            git_reader.GitReader, "read_files", autospec=True,
            side_effect=git_reader.GitReaderError("unreachable"),
        ) as read, patch.object(
            _repo_requirements, "safe_urlopen", autospec=True,
        ) as github:
            self.assertIsNone(fetch_requirements_txt(self.env, url, "main"))
        self.assertIsNone(read.call_args.args[2])  # public: no credential
        github.assert_not_called()
