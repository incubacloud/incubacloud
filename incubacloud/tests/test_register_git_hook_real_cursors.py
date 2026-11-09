"""Registrations and rotations of one GitLab webhook on real transactions (G3, §4.7.2).

Two users press «Register webhook» for the same path at once, or a retry
comes in while the first is still running. The race that mattered: with
the row already there and no ``remote_hook_id`` yet (a registration that
fell back to manual, or one whose GitLab call failed), both requests
read it, found no webhook of the panel in GitLab and created one each,
so GitLab delivered every push twice. A serialization failure in
between did not stop that: Odoo's HTTP layer retries it, and the retry
created its webhook too. The route now finds the row on a READ
COMMITTED cursor of its own and holds it ``FOR NO KEY UPDATE`` while it
talks to GitLab: the second request waits, reads the first's
``remote_hook_id`` and adopts its webhook. Rotating the secret takes the
same lock.

The transactions here are real, as in ``test_gitlab_receiver_real_cursors``:
a fixture is committed (and removed after); the concurrent requests run
in threads, each on its own cursor.
"""
import threading
import time
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, api
from odoo import http as odoo_http
from odoo.addons.incubacloud.controllers import data_load as data_load_module
from odoo.addons.incubacloud.controllers._data_load import _routes_git_hooks
from odoo.addons.incubacloud.gitlab.client import GitLabClient
from odoo.addons.incubacloud.models import cloud_gitlab_hook
from odoo.http import Request
from odoo.modules.registry import DummyRLock, Registry
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

    def _commit_hook(self, **vals):
        """Commit a webhook row of the path with *vals*; return its id.

        :rtype: int
        """
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            hook = env["cloud.gitlab.hook"].create(
                {"provider_id": self.provider_id, "path": _PATH} | vals,
            )
            cr.commit()
            return hook.id

    def _waiting_on_a_lock(self):
        """Return whether a backend of this database waits on a lock.

        A fresh cursor each time: ``pg_stat_activity`` is read once per
        transaction.

        :rtype: bool
        """
        with self.env.registry.cursor() as cr:
            cr.execute("""
                SELECT count(*) FROM pg_stat_activity
                 WHERE datname = current_database() AND wait_event_type = 'Lock'
            """)
            return bool(cr.fetchone()[0])

    def _in_thread(self, func):
        """Run *func* on a request cursor of its own in a thread.

        :return: the thread, and a list that receives what *func* returns
        :rtype: tuple
        """
        out = []
        # The test runner holds the registry's lock while tests run: a
        # thread building an environment would wait on it for ever. Odoo's
        # own registry test mode lifts it the same way.
        patcher = patch.object(Registry, "_lock", DummyRLock())
        patcher.start()
        self.addCleanup(patcher.stop)

        def run():
            """Call *func* on a cursor of this thread, keep its answer, commit."""
            with self.env.registry.cursor() as cr:
                out.append(func(cr))
                cr.commit()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, out

    def _register_in_thread(self, cr):
        """Serve ``register_git_hook`` on *cr* under a request of this thread only.

        ``odoo.http.request`` reads a context-local stack: each thread
        pushes its own stand-in, so neither sees the other's cursor.

        :rtype: dict
        """
        env = api.Environment(cr, SUPERUSER_ID, {})
        controller = data_load_module.CloudDataLoadController()
        controller._sec = lambda: env["cloud.security.mixin"]
        fake_request = MagicMock(spec=Request)
        fake_request.env = env
        odoo_http._request_stack.push(fake_request)
        try:
            return controller.cloud_register_git_hook(
                line_model="project", line_id=self.line_id,
            )
        finally:
            odoo_http._request_stack.pop()

    @staticmethod
    def _until(condition, seconds=10):
        """Wait until *condition()* holds, *seconds* at most; return whether it did.

        :rtype: bool
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def test_two_registrations_at_once_create_one_webhook(self):
        """The row is there without remote_hook_id: the second waits and adopts."""
        self._commit_hook()
        in_gitlab, release = threading.Event(), threading.Event()

        def create(*_args):
            """Hold the first creation in GitLab until the test lets it go."""
            in_gitlab.set()
            release.wait(10)
            self.api.get_hook.return_value = {"id": 9}
            return {"id": 9}

        self.api.create_hook.side_effect = create
        first, first_out = self._in_thread(self._register_in_thread)
        self.assertTrue(in_gitlab.wait(10))
        second, second_out = self._in_thread(self._register_in_thread)
        # The second waits on the row; without the lock it reaches GitLab too.
        self.assertTrue(self._until(
            lambda: self._waiting_on_a_lock() or self.api.create_hook.call_count > 1,
        ))
        release.set()
        first.join(20)
        second.join(20)
        self.assertTrue(first_out and first_out[0]["ok"])
        self.assertTrue(second_out and second_out[0]["ok"])
        self.assertEqual(self.api.create_hook.call_count, 1)
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT count(*), max(remote_hook_id) FROM cloud_gitlab_hook"
                " WHERE provider_id = %s AND path = %s",
                (self.provider_id, _PATH),
            )
            self.assertEqual(cr.fetchone(), (1, 9))

    def test_a_rotation_waits_for_the_registration_holding_the_row(self):
        """GitLab gets one PUT at a time: the rotation calls it once it holds the row."""
        hook_id = self._commit_hook(gitlab_id=77, remote_hook_id=9, state="active")

        def rotate(cr):
            """Rotate the webhook's secret from a request on *cr*."""
            env = api.Environment(cr, SUPERUSER_ID, {})
            return env["cloud.gitlab.hook"].browse(hook_id)._rotate_secret()

        with self.env.registry.cursor() as holder:
            holder.execute(
                "SELECT id FROM cloud_gitlab_hook WHERE id = %s FOR NO KEY UPDATE",
                (hook_id,),
            )
            thread, out = self._in_thread(rotate)
            self.assertTrue(self._until(
                lambda: self._waiting_on_a_lock() or self.api.update_hook.called,
            ))
            self.api.update_hook.assert_not_called()
            holder.commit()
        thread.join(20)
        self.assertEqual(self.api.update_hook.call_count, 1)
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.assertEqual(env["cloud.gitlab.hook"].browse(hook_id).secret, out[0])
