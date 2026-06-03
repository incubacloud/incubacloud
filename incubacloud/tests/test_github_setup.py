"""Pure-Python unit tests for GitHubSetupController._github_app_name.

GitHub App names must be globally unique (across the whole IncubaCloud
fleet) and at most 34 chars. The name is derived from the tenant's host
subdomain, so two tenants sharing a company name still get distinct app
names.
"""
from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.controllers.github_setup import (
    GitHubSetupController,
)


class TestGitHubAppName(BaseCase):

    def _name(self, base_url, fallback=""):
        return GitHubSetupController._github_app_name(base_url, fallback)

    def test_uses_host_subdomain(self):
        self.assertEqual(
            self._name("https://acme.incubacloud.io"),
            "IncubaCloud-acme",
        )

    def test_distinct_per_tenant_subdomain(self):
        # Same base domain, different subdomain → different app names.
        a = self._name("https://t1.incubacloud.io")
        b = self._name("https://t2.incubacloud.io")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "IncubaCloud-t1")
        self.assertEqual(b, "IncubaCloud-t2")

    def test_sanitises_non_alphanumerics(self):
        self.assertEqual(
            self._name("https://my_tenant--x.example.com"),
            "IncubaCloud-my-tenant-x",
        )

    def test_caps_at_34_chars(self):
        name = self._name(
            "https://a-very-long-subdomain-name-indeed.example.com",
        )
        self.assertLessEqual(len(name), 34)
        self.assertTrue(name.startswith("IncubaCloud-"))

    def test_falls_back_to_provided_host(self):
        # No hostname in base_url → use the fallback host header.
        self.assertEqual(
            self._name("", "beta.incubacloud.io"),
            "IncubaCloud-beta",
        )

    def test_falls_back_to_app_when_no_label(self):
        self.assertEqual(self._name("", ""), "IncubaCloud-app")
