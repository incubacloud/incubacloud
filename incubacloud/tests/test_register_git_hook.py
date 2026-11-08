"""Registering, verifying and rotating GitLab webhooks (G3, §4.7.2, §4.7.4)."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.incubacloud.controllers._data_load import _routes_git_hooks
from odoo.addons.incubacloud.gitlab.client import GitLabClient, GitLabError
from odoo.addons.incubacloud.models import cloud_gitlab_hook
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .test_git_provider_routes import _RoutesCase


@tagged("post_install", "-at_install")
class TestRegisterGitHook(_RoutesCase):

    def setUp(self):
        """A project with a GitLab line, a credential with api, a mocked API client."""
        super().setUp()
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
        patcher.start()
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
        hook_id = self._register()["hook"]["id"]
        hooks = self._serve("cloud_get_git_hooks", user=self.consultant)["hooks"]
        row = next(h for h in hooks if h["id"] == hook_id)
        self.assertFalse(row["last_delivery_at"])
        self.assertFalse(row["url"], "the URL is shown to managers only")


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
        other = self.hook.copy({"path": "acme/web2", "remote_hook_id": 10})
        self.api.get_hook.side_effect = [GitLabError("unreachable"), {"alert_status": "disabled"}]
        self.env["cloud.git.provider"]._cron_check_git_providers()
        self.assertTrue(self.env["cloud.alert"].search([
            ("code", "=", "gitlab_hook_failing"), ("state", "=", "active"),
            ("message", "ilike", "acme/web2"),
        ]))

