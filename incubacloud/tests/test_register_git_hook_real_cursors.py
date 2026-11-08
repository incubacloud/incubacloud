"""Two registrations of one GitLab webhook on real transactions (G3, §4.7.2).

Two users press «Register webhook» for the same path at once, or a retry
comes in while the first is still running. The second request's
REPEATABLE READ snapshot can predate the first's commit: its own
``INSERT … ON CONFLICT DO NOTHING`` then meets a row it cannot see, and
before the fix it answered 500 and could leave GitLab with two
webhooks. The route now finds the row on a READ COMMITTED cursor of its
own, holds it ``FOR NO KEY UPDATE`` while it talks to GitLab, and the
second request adopts the webhook the first created.

The transactions here are real, as in ``test_gitlab_receiver_real_cursors``:
a fixture is committed (and removed after), the late request takes its
snapshot, the first registers and commits, then the late one registers.
"""
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, api
from odoo import http as odoo_http
from odoo.addons.incubacloud.controllers import data_load as data_load_module
from odoo.addons.incubacloud.controllers._data_load import _routes_git_hooks
from odoo.addons.incubacloud.gitlab.client import GitLabClient
from odoo.addons.incubacloud.models import cloud_gitlab_hook
from odoo.http import Request
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_PATH = "acme/register-rc"


@tagged("post_install", "-at_install")
class TestRegisterGitHookRealCursors(TransactionCase):

    def setUp(self):
        """Commit a provider, an api credential and a project line; removed on cleanup."""
        super().setUp()
        patcher = patch.object(
            type(self.env["cloud.git.provider"]), "_network_policy",
            autospec=True, return_value="any",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["ir.config_parameter"].sudo().set_param(
                "web.base.url", "https://panel.example.com",
            )
            provider = env["cloud.git.provider"].create({
                "name": "GL register rc", "base_url": "https://gitlab.register-rc.test",
            })
            env["cloud.git.credential"].create({
                "provider_id": provider.id, "namespace": "acme", "kind": "project",
                "token": "glpat-registerrc1", "scopes": "api",
            })
            project = env["cloud.project"].create({"name": "register-rc"})
            line = env["cloud.project.repo"].create({
                "project_id": project.id, "branch": "main",
                "url": f"https://gitlab.register-rc.test/{_PATH}.git",
            })
            self.provider_id, self.project_id, self.line_id = (
                provider.id, project.id, line.id,
            )
            cr.commit()
        self.addCleanup(self._drop)
        self.api = MagicMock(spec=GitLabClient)
        self.api.get_project.return_value = {"id": 77}
        self.api.list_hooks.return_value = []
        self.api.create_hook.return_value = {"id": 9}
        self.api.get_hook.return_value = {"id": 9}
        patcher = patch.object(
            cloud_gitlab_hook, "GitLabClient", autospec=True, return_value=self.api,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _drop(self):
        """Delete what the test committed, children first."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM cloud_gitlab_hook WHERE provider_id = %s", (self.provider_id,),
            )
            cr.execute("DELETE FROM cloud_project_repo WHERE id = %s", (self.line_id,))
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["cloud.project"].browse(self.project_id).unlink()
            cr.execute(
                "DELETE FROM cloud_git_credential WHERE provider_id = %s", (self.provider_id,),
            )
            cr.execute("DELETE FROM cloud_git_provider WHERE id = %s", (self.provider_id,))
            cr.commit()

    def _register(self, cr):
        """Serve ``register_git_hook`` for the line on the request cursor *cr*.

        :rtype: dict
        """
        env = api.Environment(cr, SUPERUSER_ID, {})
        controller = data_load_module.CloudDataLoadController()
        controller._sec = lambda: env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = env
        with patch.object(odoo_http, "request", fake_request), \
                patch.object(_routes_git_hooks, "request", fake_request):
            return controller.cloud_register_git_hook(
                line_model="project", line_id=self.line_id,
            )

    def test_a_registration_whose_snapshot_predates_the_first_adopts_its_webhook(self):
        with self.env.registry.cursor() as late:
            late.execute("SELECT 1")  # the late request's snapshot is taken here
            with self.env.registry.cursor() as first:
                answer_first = self._register(first)
                first.commit()
            answer_late = self._register(late)
            late.commit()
        self.assertTrue(answer_first["ok"])
        self.assertTrue(answer_late["ok"])
        self.assertEqual(answer_first["hook"]["id"], answer_late["hook"]["id"])
        self.assertEqual(self.api.create_hook.call_count, 1)
        self.assertEqual(self.api.update_hook.call_count, 1)
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT count(*), max(remote_hook_id) FROM cloud_gitlab_hook"
                " WHERE provider_id = %s AND path = %s",
                (self.provider_id, _PATH),
            )
            self.assertEqual(cr.fetchone(), (1, 9))
