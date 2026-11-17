"""The GitHub App can be created under an organization.

The setup page asks where GitHub creates the App: the signed-in account,
as before, or an organization the user owns or manages, named as in
GitHub's URL. The manifest then goes to that organization's page for
new Apps. The name only ever fills the organization's segment of that
address: anything that is not a GitHub login is refused before a URL is
built. GitHub itself refuses a user who may not create Apps there.
"""
import html
import http.client
import json
import re
import urllib.parse
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase

from odoo.addons.incubacloud.controllers import github_setup

# Valid GitHub logins: letters, digits and single hyphens, none first or
# last, 39 characters at most.
_GOOD_NAMES = ("a", "acme", "Acme-Co", "a1-b2-c3", "9lives", "a" * 39)
# Not logins, and attempts to steer the URL somewhere else.
_BAD_NAMES = (
    "",
    "-acme",
    "acme-",
    "ac--me",
    "a" * 40,
    "acme/evil",
    "acme/../../settings",
    "..",
    "../acme",
    "acme?state=forged",
    "acme#frag",
    "ac me",
    " acme",
    "acme\n",
    "acme\t",
    "acme%2Fevil",
    "acme.io",
    "acme_co",
    "acme@evil.example",
    "acme\\evil",
    "github.com/acme",
    "https://evil.example/",
    "//evil.example",
    "acmé",
    "аcme",  # Cyrillic a
    "ａcme",  # fullwidth a
    "acme​",  # zero-width space
    'acme"><b>x',
)


class TestTheAppCreationUrl(BaseCase):
    """Where the manifest goes, built from the state and the owner chosen."""

    def test_my_account_keeps_the_old_url(self):
        self.assertEqual(
            github_setup._github_app_new_url("1.n0nce.s1g"),
            "https://github.com/settings/apps/new?state=1.n0nce.s1g",
        )

    def test_an_organization_has_its_own_url(self):
        self.assertEqual(
            github_setup._github_app_new_url("1.n0nce.s1g", "acme-co"),
            "https://github.com/organizations/acme-co/settings/apps/new"
            "?state=1.n0nce.s1g",
        )

    def test_every_valid_name_fills_only_its_segment(self):
        for name in _GOOD_NAMES:
            with self.subTest(name=name):
                url = urllib.parse.urlsplit(
                    github_setup._github_app_new_url("s", name),
                )
                self.assertEqual((url.scheme, url.netloc), ("https", "github.com"))
                self.assertEqual(
                    url.path, f"/organizations/{name}/settings/apps/new",
                )
                self.assertEqual(url.query, "state=s")
                self.assertEqual(url.fragment, "")

    def test_a_name_that_is_not_a_github_login_is_refused(self):
        for name in _BAD_NAMES:
            with self.subTest(name=name), self.assertRaises(ValueError):
                github_setup._github_app_new_url("s", name)


@tagged("-at_install", "post_install")
class TestTheSetupPageChoice(HttpCase):
    """The setup page's choice, and what its form hands GitHub."""

    def setUp(self):
        """A manager of the panel, who may set GitHub up."""
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "owner-manager",
            "login": "owner-manager",
            "password": "owner-manager",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_manager").id,
            ])],
        })
        self.authenticate("owner-manager", "owner-manager")

    def _page(self):
        """The setup page, as the browser gets it behind the proxy."""
        response = self.url_open(
            "/cloud/github/setup", headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(response.status_code, 200)
        return response.text

    def _continue(self, **fields):
        """Submit the setup page's form with *fields*; the answer's text."""
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', self._page(),
        ).group(1)
        response = self.url_open(
            "/cloud/github/setup",
            data={"csrf_token": token} | fields,
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(response.status_code, 200)
        return response.text

    def _github_action(self, text):
        """The address the answer's form posts the manifest to, or None."""
        found = re.search(r'<form id="f" method="post" action="([^"]+)"', text)
        action = html.unescape(found.group(1)) if found else ""
        return action if action.startswith("https://github.com/") else None

    def _state_of(self, action):
        """The state *action* carries, checked as the callback checks it."""
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(action).query)["state"][0]
        self.assertIsNone(github_setup._state_problem(self.env, self.user.id, state))
        return state

    def test_the_page_offers_the_choice(self):
        text = self._page()
        self.assertRegex(
            text, r'<input[^>]*name="owner"[^>]*value="user"[^>]*checked',
        )
        self.assertRegex(text, r'<input[^>]*name="owner"[^>]*value="org"')
        self.assertNotRegex(
            text, r'<input[^>]*name="owner"[^>]*value="org"[^>]*checked',
        )
        self.assertRegex(text, r'<input[^>]*name="org"')
        self.assertIn("Where to create it", text)
        self.assertIn("My account", text)
        self.assertIn("An organization", text)
        # The page itself sends nothing to GitHub: its form comes back here.
        self.assertIn('action="/cloud/github/setup"', text)
        self.assertIsNone(self._github_action(text))
        self.assertNotIn("data-m=", text)

    def test_my_account_goes_to_the_old_url(self):
        text = self._continue(owner="user")
        action = self._github_action(text)
        self.assertTrue(action, text)
        self.assertTrue(
            action.startswith("https://github.com/settings/apps/new?state="),
            action,
        )
        self._state_of(action)
        self.assertIn("data-m=", text)

    def test_an_organization_goes_to_its_own_url(self):
        action = self._github_action(self._continue(owner="org", org="acme-co"))
        self.assertTrue(
            action.startswith(
                "https://github.com/organizations/acme-co/settings/apps/new?state=",
            ),
            action,
        )
        self._state_of(action)

    def test_spaces_around_the_name_are_dropped(self):
        action = self._github_action(self._continue(owner="org", org="  acme-co "))
        self.assertTrue(
            action.startswith("https://github.com/organizations/acme-co/"),
            action,
        )

    def test_the_name_is_ignored_for_my_account(self):
        action = self._github_action(
            self._continue(owner="user", org="evil/../../x"),
        )
        self.assertTrue(
            action.startswith("https://github.com/settings/apps/new?state="),
            action,
        )

    def test_a_bad_name_goes_nowhere(self):
        for name in _BAD_NAMES:
            # Spaces around a name are dropped (above); none at all is below.
            if name.strip() != name or not name:
                continue
            with self.subTest(name=name):
                text = self._continue(owner="org", org=name)
                self.assertIsNone(self._github_action(text))
                self.assertNotIn("data-m=", text)
                self.assertIn("is not a GitHub organization name", text)
                # The choice stays on the organization, with its name to fix.
                self.assertRegex(
                    text, r'<input[^>]*name="owner"[^>]*value="org"[^>]*checked',
                )
                self.assertNotIn('"><b>x', text)

    def test_an_organization_needs_a_name(self):
        text = self._continue(owner="org", org="  ")
        self.assertIsNone(self._github_action(text))
        self.assertIn("Type the organization's name", html.unescape(text))


@tagged("-at_install", "post_install")
class TestTheAppOwnerIsKept(HttpCase):
    """The callback keeps who owns the App, which Settings → GitHub shows."""

    def setUp(self):
        """A manager of the panel and no App yet."""
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "owner-callback",
            "login": "owner-callback",
            "password": "owner-callback",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_manager").id,
            ])],
        })
        self.env["cloud.github.app"].search([]).unlink()
        self.authenticate("owner-callback", "owner-callback")

    def _create(self, owner):
        """Come back from GitHub with an App owned by *owner*; the App kept."""
        answer = MagicMock(spec=http.client.HTTPResponse)
        answer.__enter__.return_value = answer
        answer.read.return_value = json.dumps({
            "id": 778,
            "pem": "not a key",
            "webhook_secret": "s",
            "slug": "incubacloud-y",
            "owner": owner,
        }).encode()
        state = github_setup._make_state(self.env, self.user.id)
        with patch.object(github_setup, "safe_urlopen", return_value=answer):
            response = self.url_open(
                f"/cloud/github/callback?code=c0de&state={state}",
                allow_redirects=False,
            )
        self.assertIn("setup_ok=1", response.headers.get("Location", ""))
        return self.env["cloud.github.app"].search([])

    def _settings(self):
        """What Settings → GitHub reads about the App."""
        response = self.url_open(
            "/cloud/get_github_app",
            json={"jsonrpc": "2.0", "method": "call", "params": {}},
        )
        return response.json()["result"]

    def test_an_organization_is_kept_as_one(self):
        app = self._create({"login": "acme-co", "type": "Organization"})
        self.assertEqual(app.owner_login, "acme-co")
        self.assertEqual(app.owner_type, "organization")
        settings = self._settings()
        self.assertEqual(settings["owner_login"], "acme-co")
        self.assertEqual(settings["owner_type"], "organization")

    def test_a_user_account_is_kept_as_one(self):
        app = self._create({"login": "operator", "type": "User"})
        self.assertEqual(app.owner_type, "user")
        self.assertEqual(self._settings()["owner_type"], "user")

    def test_no_owner_in_the_answer_keeps_none(self):
        app = self._create(None)
        self.assertFalse(app.owner_login)
        self.assertFalse(app.owner_type)
        settings = self._settings()
        self.assertEqual(settings["owner_login"], "")
        self.assertEqual(settings["owner_type"], "")
