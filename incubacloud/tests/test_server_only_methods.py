"""The GitHub credentials are handed to the panel's code, not to RPC.

Odoo's ``/web/dataset/call_kw`` runs any public method of a model for an
internal user. The GitHub credential service hands out the personal
access token, the GitHub App's credentials and the webhook secret: the
panel's routes, executors and webhook call it, ``call_kw`` refuses it.
"""

import hashlib
import hmac
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.service.model import call_kw, get_public_method

from .test_cloud_security import CloudSecurityBase

SERVICE = "cloud.github.credential.service"

#: What the credential service answers, with arguments that would work.
SERVICE_METHODS = (
    ("get_pat", []),
    ("get_credentials", []),
    ("resolve_webhook_secret", [b"{}", "sha256=0"]),
    ("test_connection", []),
)

#: Every role of the panel, by the group each one is given.
ROLES = {
    "stakeholder": "group_cloud_user",
    "consultant": "group_cloud_consultant",
    "pm": "group_cloud_project_manager",
    "developer": "group_cloud_developer",
    "manager": "group_cloud_manager",
}

PAT = "ghp_server_only_0123456789abcdef"
WEBHOOK_SECRET = "server-only-webhook-secret"


class TestServerOnlyMethods(CloudSecurityBase):
    """One user per role; a PAT and a GitHub App with a webhook secret."""

    def setUp(self):
        """Create the users and configure the GitHub credentials."""
        super().setUp()
        self.users = {
            role: self._create_user(f"server_only_{role}", group)
            for role, group in ROLES.items()
        }
        self.env["cloud.settings"]._get_system().github_pat = PAT
        self.env["cloud.github.app"].sudo().search([]).unlink()
        self.env["cloud.github.app"].sudo().create({
            "app_id": "12345",
            "installation_id": "67890",
            "private_key": "test-key",
            "webhook_secret": WEBHOOK_SECRET,
        })

    def test_no_role_reaches_the_credential_service(self):
        """Refused by ``call_kw`` for every role, an Administrator's too."""
        for role, user in self.users.items():
            service = self.env[SERVICE].with_user(user)
            for method, args in SERVICE_METHODS:
                with self.subTest(role=role, method=method):
                    with self.assertRaises(AccessError):
                        get_public_method(service, method)
                    with self.assertRaises(AccessError):
                        call_kw(service, method, args, {})

    def test_the_panel_s_code_still_reads_the_credentials(self):
        """Called from Python, the service answers as it did."""
        service = self.env[SERVICE].with_user(self.users["stakeholder"])
        self.assertEqual(service.get_pat(), PAT)
        self.assertTrue(service.get_credentials())
        payload = b'{"zen": "ok"}'
        signature = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), payload, hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            service.resolve_webhook_secret(payload, signature), WEBHOOK_SECRET,
        )
        self.assertEqual(service.resolve_webhook_secret(payload, "sha256=0"), "")

    def test_the_backend_connection_test_is_an_administrator_s(self):
        """As the panel's settings route: below Administrator, refused."""
        app = self.env["cloud.github.app"].sudo().search([], limit=1)
        answer = {"ok": True, "app_name": "QA app", "app_id": "12345"}
        with patch(
            "odoo.addons.incubacloud.models.cloud_github_credential_service"
            ".GitHubAppClient",
            autospec=True,
        ) as client:
            client.return_value.test_connection.return_value = answer
            for role in ("stakeholder", "consultant", "pm", "developer"):
                with self.subTest(role=role):
                    with self.assertRaises(AccessError):
                        call_kw(
                            app.with_user(self.users[role]),
                            "action_test_connection", [app.ids], {},
                        )
            result = call_kw(
                app.with_user(self.users["manager"]),
                "action_test_connection", [app.ids], {},
            )
        self.assertEqual(result["params"]["type"], "success")
