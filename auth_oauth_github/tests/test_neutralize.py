"""A restored copy of production keeps no GitHub credential.

Stock ``auth_oauth`` neutralization only switches the providers off. On
7-oct-2026 the QA copy of production still held the GitHub OAuth App's
client secret and a user's GitHub access token, in plain text: GitHub's
tokens do not expire, so either one worked from the copy as well as from
production. This module's ``data/neutralize.sql`` clears them, and the
test runs that very file.
"""
import re

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.misc import file_open


@tagged("post_install", "-at_install")
class TestNeutralizeClearsGitHubCredentials(TransactionCase):

    def setUp(self):
        super().setUp()
        self.github = self.env.ref("auth_oauth_github.provider_github")
        self.github.write({
            "client_id": "Iv1.public-client-id",
            "client_secret": "prod-client-secret",
        })
        self.other = self.env["auth.oauth.provider"].create({
            "name": "Some other provider",
            "client_id": "other-client",
            "auth_endpoint": "https://idp.example.test/auth",
            "validation_endpoint": "https://idp.example.test/me",
            "body": "Sign in elsewhere",
        })
        Users = self.env["res.users"].with_context(no_reset_password=True)
        self.github_user = Users.create({
            "name": "Signed in with GitHub",
            "login": "neutralize-github@example.test",
            "oauth_provider_id": self.github.id,
            "oauth_uid": "4242",
            "oauth_access_token": "gho_real_token",
        })
        self.other_user = Users.create({
            "name": "Signed in elsewhere",
            "login": "neutralize-other@example.test",
            "oauth_provider_id": self.other.id,
            "oauth_uid": "other-1",
            "oauth_access_token": "other-token",
        })

    def _neutralize(self):
        """Run this module's ``data/neutralize.sql``, statement by statement."""
        with file_open("auth_oauth_github/data/neutralize.sql") as handle:
            body = re.sub(r"--[^\n]*", "", handle.read())
        for statement in body.split(";"):
            if statement.strip():
                self.env.cr.execute(statement)
        self.env.invalidate_all()

    def _token(self, user):
        """Read ``oauth_access_token`` as stored, past its field groups."""
        self.env.cr.execute(
            "SELECT oauth_access_token FROM res_users WHERE id = %s", (user.id,)
        )
        return self.env.cr.fetchone()[0]

    def test_the_client_secret_goes_and_the_client_id_stays(self):
        self._neutralize()
        self.assertFalse(self.github.client_secret)
        self.assertEqual(self.github.client_id, "Iv1.public-client-id")

    def test_github_users_lose_their_token(self):
        self._neutralize()
        self.assertFalse(self._token(self.github_user))
        self.assertEqual(self.github_user.oauth_uid, "4242")

    def test_other_providers_are_left_to_their_own_modules(self):
        self._neutralize()
        self.assertEqual(self._token(self.other_user), "other-token")
