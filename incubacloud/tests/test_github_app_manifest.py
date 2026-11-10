"""The GitHub App a panel creates may comment on pull requests.

A pull request preview leaves a comment with its URL on the pull request
(``cloud.instance._post_or_update_pr_comment``), which GitHub only allows
an App with write access to pull requests. The manifest asked for read,
so every comment failed with a 403 that only reached the log (B6,
8-oct-2026).
"""
import base64
import http.client
import json
import re
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.controllers import github_setup


@tagged("-at_install", "post_install")
class TestGitHubAppManifest(HttpCase):

    def setUp(self):
        """A manager of the panel, who may set GitHub up."""
        super().setUp()
        self.env["res.users"].create({
            "name": "manifest-manager",
            "login": "manifest-manager",
            "password": "manifest-manager",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_manager").id,
            ])],
        })

    def _manifest(self):
        """The manifest the setup page hands to GitHub."""
        self.authenticate("manifest-manager", "manifest-manager")
        response = self.url_open(
            "/cloud/github/setup", headers={"X-Forwarded-Proto": "https"}
        )
        self.assertEqual(response.status_code, 200)
        found = re.search(r'data-m="([^"]+)"', response.text)
        self.assertTrue(found, "the setup page carries no manifest")
        return json.loads(base64.b64decode(found.group(1)))

    def test_the_app_may_comment_on_pull_requests(self):
        permissions = self._manifest()["default_permissions"]
        self.assertEqual(permissions["pull_requests"], "write")
        self.assertEqual(permissions["contents"], "read")

    def test_the_callback_keeps_the_account_that_owns_the_app(self):
        """The installation the webhook takes by itself is that account's:
        the App is public, and anyone may install it on theirs."""
        self.env["cloud.github.app"].search([]).unlink()
        manager = self.env["res.users"].search([("login", "=", "manifest-manager")])
        answer = MagicMock(spec=http.client.HTTPResponse)
        answer.__enter__.return_value = answer
        answer.read.return_value = json.dumps({
            "id": 777,
            "pem": "not a key",
            "webhook_secret": "s",
            "slug": "incubacloud-x",
            "owner": {"login": "operator", "type": "User"},
        }).encode()
        self.authenticate("manifest-manager", "manifest-manager")
        state = github_setup._make_state(self.env, manager.id)
        with patch.object(github_setup, "safe_urlopen", return_value=answer):
            response = self.url_open(
                f"/cloud/github/callback?code=c0de&state={state}",
                allow_redirects=False,
            )
        self.assertIn("setup_ok=1", response.headers.get("Location", ""))
        app = self.env["cloud.github.app"].search([])
        self.assertEqual(app.owner_login, "operator")
        self.assertFalse(app.installation_id)
