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

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_crud,
    _routes_github,
    _routes_ops,
)
from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController

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
        reads = [("project_name: Refused host import\n", ""), (repos_yaml, "")]
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
