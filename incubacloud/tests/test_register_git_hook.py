"""Registering, verifying and rotating GitLab webhooks (G3, §4.7.2, §4.7.4)."""
import http.client
import json
import socket
import ssl
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.incubacloud.controllers._data_load import _routes_git_hooks
from odoo.addons.incubacloud.gitlab import client as client_mod
from odoo.addons.incubacloud.gitlab.client import GitLabClient, GitLabError
from odoo.addons.incubacloud.models import cloud_gitlab_hook
from odoo.addons.incubacloud.models._concurrency import read_committed_cursor
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .test_git_provider_routes import _RoutesCase


@tagged("post_install", "-at_install")
class TestRegisterGitHook(_RoutesCase):

    def setUp(self):
        """A project with a GitLab line, a credential with api, a mocked API client.

        The registry is in test mode: registering and «Verify» write on a
        READ COMMITTED cursor of their own, which then rides on the test's.
        """
        super().setUp()
        self.registry_enter_test_mode()
        # The routes run under a stand-in request, which carries no test
        # cursor cookie.
        self.http_request_allow_all = True
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://panel.example.com",
        )
        self._cred("acme").sudo().write({"scopes": "api"})
        self.project = self.env["cloud.project"].create({"name": "Hooks"})
        self.line = self.env["cloud.project.repo"].create({
            "project_id": self.project.id,
            "url": "https://gitlab.com/acme/web.git", "branch": "main",
        })
        self.api = MagicMock(spec=GitLabClient)
        self.api.get_project.return_value = {"id": 55}
        self.api.list_hooks.return_value = []
        self.api.create_hook.return_value = {"id": 9}
        patcher = patch.object(
            cloud_gitlab_hook, "GitLabClient", autospec=True, return_value=self.api,
        )
        self.client_cls = patcher.start()
        self.addCleanup(patcher.stop)

    def _register(self, **kwargs):
        """Call register_git_hook for the line.

        :rtype: dict
        """
        return self._serve(
            "cloud_register_git_hook", line_model="project",
            line_id=self.line.id, **kwargs,
        )

    def _serve(self, route, user=None, **kwargs):
        """Serve *route* with the webhook routes' request patched too.

        :rtype: dict
        """
        env = self.env(user=user) if user else self.env
        env.flush_all()
        with patch.object(_routes_git_hooks, "request", MagicMock(env=env)):
            return super()._serve(route, user=user, **kwargs)

    def test_the_api_creates_the_webhook_with_url_and_token(self):
        answer = self._register()
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["manual"])
        hook = self.env["cloud.gitlab.hook"].browse(answer["hook"]["id"])
        self.assertEqual((hook.path, hook.gitlab_id, hook.remote_hook_id), ("acme/web", 55, 9))
        self.assertRegex(hook.hook_key, r"^[0-9a-f]{32}$")
        kind, target, url, token = self.api.create_hook.call_args.args
        self.assertEqual((kind, target), ("projects", 55))
        self.assertEqual(url, f"https://panel.example.com/cloud/gitlab/webhook/{hook.hook_key}")
        self.assertEqual(token, hook.secret)

    def test_registering_twice_keeps_one_row_and_one_remote_webhook(self):
        first = self._register()["hook"]["id"]
        self.api.get_hook.return_value = {"id": 9}
        second = self._register()["hook"]["id"]
        self.assertEqual(first, second)
        self.assertEqual(self.api.create_hook.call_count, 1)
        self.assertEqual(self.env["cloud.gitlab.hook"].search_count([("path", "=", "acme/web")]), 1)

    def test_the_panels_webhook_already_in_gitlab_is_adopted(self):
        self.api.list_hooks.return_value = [
            {"id": 3, "url": "https://other-panel.example.com/cloud/gitlab/webhook/" + "1" * 32},
            {"id": 4, "url": "https://panel.example.com/cloud/gitlab/webhook/" + "2" * 32},
        ]
        answer = self._register()
        hook = self.env["cloud.gitlab.hook"].browse(answer["hook"]["id"])
        self.api.create_hook.assert_not_called()
        self.assertEqual(self.api.update_hook.call_args.args[2], 4)
        self.assertEqual(self.api.update_hook.call_args.args[4], hook.secret)
        self.assertEqual(hook.remote_hook_id, 4)

    def test_a_403_falls_back_to_manual_with_its_reason(self):
        self.api.create_hook.side_effect = GitLabError("forbidden", 403)
        answer = self._register()
        self.assertTrue(answer["manual"])
        self.assertIn("Maintainer", answer["reason"])

    def test_rotating_rewrites_the_remote_with_url_and_token(self):
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        old = hook.secret
        answer = self._serve("cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True)
        self.assertNotEqual(answer["secret"], old)
        _kind, _target, _id, url, token = self.api.update_hook.call_args.args
        self.assertEqual((url, token), (hook._url(), answer["secret"]))

    def test_the_secret_is_for_managers_only(self):
        hook_id = self._register()["hook"]["id"]
        with self.assertRaises(AccessError):
            self._serve("cloud_reveal_git_hook_secret", user=self.consultant, hook_id=hook_id)

    def test_verify_returns_at_once_keeps_15_s_and_translates_429(self):
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        answer = self._serve("cloud_verify_git_hook", hook_id=hook_id)
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["verify_requested_at"])
        self.api.test_hook.assert_called_once_with("projects", 55, 9)
        again = self._serve("cloud_verify_git_hook", hook_id=hook_id)
        self.assertFalse(again["ok"])
        self.assertEqual(self.api.test_hook.call_count, 1)
        hook.verify_requested_at = fields.Datetime.now() - timedelta(seconds=16)
        self.api.test_hook.side_effect = GitLabError("rate_limited", 429)
        limited = self._serve("cloud_verify_git_hook", hook_id=hook_id)
        self.assertIn("wait a minute", limited["error"])

    def test_get_git_hooks_shows_the_last_delivery(self):
        self.project.member_ids = self.consultant
        hook_id = self._register()["hook"]["id"]
        hooks = self._serve("cloud_get_git_hooks", user=self.consultant)["hooks"]
        row = next(h for h in hooks if h["id"] == hook_id)
        self.assertFalse(row["last_delivery_at"])
        self.assertFalse(row["url"], "the URL is shown to managers only")

    def test_get_git_hooks_lists_failed_deliveries_to_managers_only(self):
        self.project.member_ids = self.consultant
        hook_id = self._register()["hook"]["id"]
        Event = self.env["cloud.gitlab.event"].sudo()
        failed = Event.create({
            "hook_id": hook_id, "event_type": "Push Hook",
            "error": "RuntimeError: boom", "dispatch_attempts": 1,
        })
        Event.create({"hook_id": hook_id, "event_type": "Push Hook", "processed": True})
        rows = self._serve("cloud_get_git_hooks", user=self.manager)["hooks"]
        row = next(h for h in rows if h["id"] == hook_id)
        self.assertEqual([e["id"] for e in row["failed_events"]], [failed.id])
        self.assertEqual(row["failed_events"][0]["attempts"], 1)
        rows = self._serve("cloud_get_git_hooks", user=self.consultant)["hooks"]
        row = next(h for h in rows if h["id"] == hook_id)
        self.assertEqual(row["failed_events"], [])

    def test_a_rotation_gitlab_refuses_keeps_the_secret(self):
        """GitLab keeps the old secret, so the panel must too: else every delivery is a 401."""
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        old = hook.secret
        self.api.update_hook.side_effect = GitLabError("forbidden", 403)
        answer = self._serve("cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True)
        self.assertFalse(answer["ok"])
        hook.invalidate_recordset(["secret"])
        self.assertEqual(hook.secret, old)
        self.assertEqual(self.api.update_hook.call_count, 1, "a refusal is clear: no second PUT")

    def test_an_unclear_rotation_gives_gitlab_the_old_secret_back(self):
        """A timeout may hide a PUT GitLab took: the old secret is put back, as the panel keeps it."""
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        old = hook.secret
        self.api.update_hook.side_effect = [GitLabError("timeout"), {"id": 9}]
        answer = self._serve("cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True)
        self.assertFalse(answer["ok"])
        self.assertIn("did not take the new secret", answer["error"])
        hook.invalidate_recordset(["secret"])
        self.assertEqual(hook.secret, old)
        self.assertEqual(self.api.update_hook.call_count, 2)
        _kind, _target, _id, url, token = self.api.update_hook.call_args.args
        self.assertEqual((url, token), (hook._url(), old))

    def test_an_unclear_rotation_that_cannot_be_undone_says_to_rotate_again(self):
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        old = hook.secret
        for first in (GitLabError("http", 502), GitLabError("unreachable")):
            self.api.update_hook.reset_mock()
            self.api.update_hook.side_effect = [first, GitLabError("timeout")]
            with self.subTest(code=first.code):
                answer = self._serve(
                    "cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True,
                )
                self.assertFalse(answer["ok"])
                self.assertIn("Rotate secret", answer["error"])
                self.assertEqual(self.api.update_hook.call_count, 2)
                hook.invalidate_recordset(["secret"])
                self.assertEqual(hook.secret, old)

    def test_a_rotation_cut_by_the_budget_before_the_put_is_clear(self):
        """Nothing was sent: no second PUT, and the panel keeps its secret."""
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        old = hook.secret
        self.api.update_hook.side_effect = GitLabError("budget")
        answer = self._serve("cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True)
        self.assertFalse(answer["ok"])
        self.assertIn("did not take the new secret", answer["error"])
        self.assertEqual(self.api.update_hook.call_count, 1)
        hook.invalidate_recordset(["secret"])
        self.assertEqual(hook.secret, old)

    def test_a_rotation_whose_answer_is_cut_by_tls_is_unclear(self):
        """A dropped TLS session reading the answer may hide a PUT GitLab took.

        Through the real client: the connection delivers the request and
        fails reading its answer, so the old secret is put back.
        """
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        old = hook.secret
        conn = MagicMock(spec=http.client.HTTPSConnection)
        conn.sock = MagicMock(spec=socket.socket)
        answer_ok = MagicMock(spec=http.client.HTTPResponse)
        answer_ok.status = 200
        answer_ok.read1.side_effect = [b"{}", b""]
        conn.getresponse.side_effect = [ssl.SSLEOFError("EOF"), answer_ok]
        self.client_cls.return_value = GitLabClient("https://gitlab.example/api/v4", "tok")
        with patch.object(
            client_mod.http.client, "HTTPSConnection", autospec=True,
            return_value=conn,
        ):
            answer = self._serve(
                "cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True,
            )
        self.assertFalse(answer["ok"])
        self.assertEqual(conn.request.call_count, 2, "the rotation and the old secret again")
        first, second = (json.loads(c.kwargs["body"]) for c in conn.request.call_args_list)
        self.assertNotEqual(first["token"], old)
        self.assertEqual(second["token"], old)
        hook.invalidate_recordset(["secret"])
        self.assertEqual(hook.secret, old)

    def test_the_new_secret_is_written_before_gitlab_is_called(self):
        """Between GitLab taking it and the commit nothing else may fail.

        The write is flushed on the rotation's cursor before the PUT: a
        failure there rolls it back, and after the PUT only the commit is left.
        """
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)

        def stored():
            """Return the secret as the row stores it (encrypted)."""
            self.env.cr.execute(
                "SELECT secret FROM cloud_gitlab_hook WHERE id = %s", (hook_id,),
            )
            return self.env.cr.fetchone()[0]

        before = stored()
        seen = []
        self.api.update_hook.side_effect = lambda *_a: seen.append(stored())
        answer = self._serve("cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True)
        self.assertTrue(answer["ok"])
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], before, "the row held the old secret during the PUT")
        hook.invalidate_recordset(["secret"])
        self.assertEqual(hook.secret, answer["secret"])

    def test_rotating_locks_the_row_on_a_committed_cursor_before_gitlab(self):
        """The rotation takes the row as registering does, then calls GitLab."""
        hook_id = self._register()["hook"]["id"]
        order = []

        def cursor(registry):
            """Record that the rotation's cursor was opened."""
            order.append("cursor")
            return read_committed_cursor(registry)

        self.api.update_hook.side_effect = lambda *_a: order.append("gitlab")
        with patch.object(
            cloud_gitlab_hook, "read_committed_cursor", autospec=True,
            side_effect=cursor,
        ):
            answer = self._serve(
                "cloud_reveal_git_hook_secret", hook_id=hook_id, rotate=True,
            )
        self.assertTrue(answer["ok"])
        self.assertEqual(order, ["cursor", "gitlab"])

    def test_registering_gives_gitlab_one_budget_for_all_its_calls(self):
        """The row is held while GitLab is called: the calls share one deadline."""
        before = cloud_gitlab_hook.time.monotonic()
        self._register()
        deadline = self.client_cls.call_args.kwargs["deadline"]
        self.assertLessEqual(deadline - before, cloud_gitlab_hook.GITLAB_BUDGET_S + 1)
        self.assertGreater(deadline, before)

    def test_a_manual_webhook_rotates_in_the_panel_only(self):
        hook = self.env["cloud.gitlab.hook"].create({
            "provider_id": self.provider.id, "path": "acme/manual",
        })
        old = hook.secret
        answer = self._serve("cloud_reveal_git_hook_secret", hook_id=hook.id, rotate=True)
        self.assertTrue(answer["ok"])
        self.assertNotEqual(answer["secret"], old)
        self.api.update_hook.assert_not_called()

    def test_verify_stamps_its_request_on_a_committed_cursor_before_gitlab(self):
        """The delivery arrives in another request: the stamp must be committed first."""
        hook_id = self._register()["hook"]["id"]
        order = []

        def cursor(registry):
            """Record that the stamp's cursor was opened."""
            order.append("cursor")
            return read_committed_cursor(registry)

        self.api.test_hook.side_effect = lambda *_a: order.append("gitlab")
        with patch.object(
            cloud_gitlab_hook, "read_committed_cursor", autospec=True,
            side_effect=cursor,
        ):
            answer = self._serve("cloud_verify_git_hook", hook_id=hook_id)
        self.assertTrue(answer["ok"])
        self.assertEqual(order, ["cursor", "gitlab"])

    def test_get_git_hooks_shows_a_consultant_the_paths_he_reads_only(self):
        self.project.member_ids = self.consultant
        mine = self._register()["hook"]["id"]
        other_project = self.env["cloud.project"].create({"name": "Not his"})
        self.env["cloud.project.repo"].create({
            "project_id": other_project.id,
            "url": "https://gitlab.com/acme/secret.git", "branch": "main",
        })
        other = self.env["cloud.gitlab.hook"].create({
            "provider_id": self.provider.id, "path": "acme/secret",
        })
        ids = {h["id"] for h in self._serve("cloud_get_git_hooks", user=self.consultant)["hooks"]}
        self.assertEqual(ids & {mine, other.id}, {mine})
        ids = {h["id"] for h in self._serve("cloud_get_git_hooks", user=self.manager)["hooks"]}
        self.assertLessEqual({mine, other.id}, ids)

    def test_delete_without_a_credential_deletes_in_the_panel_and_warns(self):
        hook_id = self._register()["hook"]["id"]
        self.env["cloud.git.credential"].search([
            ("provider_id", "=", self.provider.id),
        ]).unlink()
        answer = self._serve("cloud_delete_git_hook", hook_id=hook_id)
        self.assertTrue(answer["ok"])
        self.assertIn("GitLab", answer["warning"])
        self.assertFalse(self.env["cloud.gitlab.hook"].browse(hook_id).exists())
        self.api.delete_hook.assert_not_called()

    def test_delete_that_gitlab_fails_keeps_the_webhook(self):
        hook_id = self._register()["hook"]["id"]
        self.api.delete_hook.side_effect = GitLabError("http", 500)
        answer = self._serve("cloud_delete_git_hook", hook_id=hook_id)
        self.assertFalse(answer["ok"])
        self.assertTrue(self.env["cloud.gitlab.hook"].browse(hook_id).exists())

    def test_delete_with_a_refused_credential_deletes_in_the_panel_and_warns(self):
        """A 401 or 403 is a credential that cannot delete it: as without one."""
        for code, status in (("auth", 401), ("forbidden", 403)):
            with self.subTest(code=code):
                hook_id = self._register()["hook"]["id"]
                self.api.delete_hook.side_effect = GitLabError(code, status)
                answer = self._serve("cloud_delete_git_hook", hook_id=hook_id)
                self.assertTrue(answer["ok"])
                self.assertIn("Delete it in GitLab by hand", answer["warning"])
                self.assertFalse(self.env["cloud.gitlab.hook"].browse(hook_id).exists())

    def test_deleting_a_failing_webhook_resolves_its_alert(self):
        hook_id = self._register()["hook"]["id"]
        hook = self.env["cloud.gitlab.hook"].browse(hook_id)
        hook.state = "failing"
        Alert = self.env["cloud.alert"].sudo()
        Alert.raise_alert(
            "gitlab_hook_failing", "disabled", key=f"gitlab_hook:{hook_id}",
        )
        answer = self._serve("cloud_delete_git_hook", hook_id=hook_id)
        self.assertTrue(answer["ok"])
        self.assertFalse(Alert.search([
            ("code", "=", "gitlab_hook_failing"), ("state", "=", "active"),
        ]))

    def test_deleting_a_webhook_resolves_its_repo_moved_alert(self):
        """The alert of a project that moved in GitLab names the webhook: it goes with it."""
        hook_id = self._register()["hook"]["id"]
        Alert = self.env["cloud.alert"].sudo()
        Alert.raise_alert(
            "gitlab_repo_moved", "moved", key=f"gitlab_hook:{hook_id}",
        )
        answer = self._serve("cloud_delete_git_hook", hook_id=hook_id)
        self.assertTrue(answer["ok"])
        self.assertFalse(Alert.search([
            ("code", "=", "gitlab_repo_moved"), ("state", "=", "active"),
        ]))


@tagged("post_install", "-at_install")
class TestGitHookCron(_RoutesCase):

    def setUp(self):
        """A panel's webhook of acme/web with a credential and a mocked API."""
        super().setUp()
        self._cred("acme").sudo().write({"scopes": "api"})
        self.hook = self.env["cloud.gitlab.hook"].create({
            "provider_id": self.provider.id, "path": "acme/web",
            "gitlab_id": 55, "remote_hook_id": 9, "state": "active",
        })
        self.env["cloud.project.repo"].create({
            "project_id": self.env["cloud.project"].create({"name": "C"}).id,
            "url": "https://gitlab.com/acme/web.git", "branch": "main",
        })
        self.api = MagicMock(spec=GitLabClient)
        patcher = patch.object(
            cloud_gitlab_hook, "GitLabClient", autospec=True, return_value=self.api,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _alert(self):
        """Return the open alert of the webhook, if any."""
        return self.env["cloud.alert"].search([
            ("code", "=", "gitlab_hook_failing"), ("state", "=", "active"),
        ])

    def test_a_disabled_webhook_fails_alerts_and_recovers(self):
        self.api.get_hook.return_value = {"id": 9, "alert_status": "temporarily_disabled"}
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertEqual(self.hook.state, "failing")
        self.assertTrue(self._alert())
        self.api.test_hook.assert_not_called()
        self.api.update_hook.assert_not_called()
        self.api.get_hook.return_value = {"id": 9, "alert_status": "executable"}
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertEqual(self.hook.state, "active")
        self.assertFalse(self._alert())

    def test_an_orphan_is_deleted_after_7_days_and_a_404_is_done(self):
        self.env["cloud.project.repo"].search([("repo_path", "=", "acme/web")]).unlink()
        self.api.get_hook.return_value = {"id": 9, "alert_status": "executable"}
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertEqual(self.hook.state, "orphan")
        self.api.delete_hook.assert_not_called()
        self.hook.orphan_since = fields.Datetime.now() - timedelta(days=8)
        self.api.delete_hook.side_effect = GitLabError("not_found", 404)
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertFalse(self.hook.exists())

    def test_one_failing_webhook_does_not_stop_the_others(self):
        self.hook.copy({"path": "acme/web2", "remote_hook_id": 10})
        self.api.get_hook.side_effect = [GitLabError("unreachable"), {"alert_status": "disabled"}]
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertTrue(self.env["cloud.alert"].search([
            ("code", "=", "gitlab_hook_failing"), ("state", "=", "active"),
            ("message", "ilike", "acme/web2"),
        ]))

    def test_an_orphan_that_keeps_receiving_pushes_is_still_deleted(self):
        """A delivery does not reset the grace of a webhook no line uses."""
        self.env["cloud.project.repo"].search([("repo_path", "=", "acme/web")]).unlink()
        since = fields.Datetime.now() - timedelta(days=8)
        self.hook.write({"state": "orphan", "orphan_since": since})
        self.hook._mark_delivered()
        self.assertEqual((self.hook.state, self.hook.orphan_since), ("orphan", since))
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertFalse(self.hook.exists())
        self.api.delete_hook.assert_called_once()

    def test_an_orphan_deleted_resolves_the_alert_it_had_when_it_failed(self):
        """Disabled by GitLab, then no line uses it: its alert goes with it."""
        self.api.get_hook.return_value = {"id": 9, "alert_status": "disabled"}
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertTrue(self._alert())
        self.env["cloud.project.repo"].search([("repo_path", "=", "acme/web")]).unlink()
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.hook.orphan_since = fields.Datetime.now() - timedelta(days=8)
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertFalse(self.hook.exists())
        self.assertFalse(self._alert())

    def test_an_orphan_used_again_is_active_when_it_had_deliveries(self):
        self.hook.write({
            "state": "orphan", "orphan_since": fields.Datetime.now() - timedelta(days=2),
        })
        self.env["cloud.gitlab.event"].sudo().create({
            "hook_id": self.hook.id, "event_type": "Push Hook", "processed": True,
        })
        self.hook._sweep_orphan()
        self.assertEqual(self.hook.state, "active")
        self.assertFalse(self.hook.orphan_since)

    def test_an_orphan_used_again_without_deliveries_is_pending(self):
        self.hook.write({
            "state": "orphan", "orphan_since": fields.Datetime.now() - timedelta(days=2),
        })
        self.hook._sweep_orphan()
        self.assertEqual(self.hook.state, "pending")

    def test_a_group_path_matches_its_underscore_literally(self):
        group = self.env["cloud.gitlab.hook"].create({
            "provider_id": self.provider.id, "scope": "group", "path": "acm_",
        })
        instance_lines, project_lines = group._lines()
        self.assertFalse(project_lines, "acm_ does not cover acme/web")
        self.assertFalse(instance_lines)

    def test_a_403_on_the_daily_check_is_logged_not_raised(self):
        """A credential without Maintainer cannot read the webhook: a warning, not a failure."""
        self.api.get_hook.side_effect = GitLabError("forbidden", 403)
        with self.assertLogs(cloud_gitlab_hook._logger.name, "WARNING") as logs:
            self.hook._check_remote_state()
        self.assertIn("acme/web", logs.output[0])
        self.assertEqual(self.hook.state, "active")
        self.assertFalse(self._alert())
