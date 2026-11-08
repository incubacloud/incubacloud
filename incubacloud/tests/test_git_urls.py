"""A GitLab repository is matched to its provider by exact authority and prefix.

The URL a token travels with is rebuilt from the provider's base and the
project path, always with ``.git``: gitlab.com answers the bare form
with a 301 and the panel follows no redirect (spike of 8-nov-2026).
Pure Python: no database.
"""
from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models._git_urls import (
    RepoRef,
    canonical_base_url,
    clone_url_with_credential,
    namespace_covers,
    normalize_namespace,
    parse_provider_repo_url,
)

_BASES = (
    "https://gitlab.com",
    "https://git.acme.tld:8443/gitlab",
)


class TestCanonicalBaseUrl(BaseCase):

    def test_canonical_forms(self):
        cases = {
            "https://GitLab.com/": "https://gitlab.com",
            "  https://gitlab.com:443  ": "https://gitlab.com",
            "https://git.acme.tld:8443/gitlab/": "https://git.acme.tld:8443/gitlab",
            "https://xn--bcher-kva.example": "https://xn--bcher-kva.example",
        }
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(canonical_base_url(typed), expected)

    def test_refused(self):
        for typed in (
            "http://gitlab.com",
            "gitlab.com",
            "https://user@gitlab.com",
            "https://user:tok@gitlab.com",
            "https://gitlab.com/?x=1",
            "https://gitlab.com/#x",
            "https://bücher.example",
            "https://gitlab.com/a/../b",
            "https://gitlab.com/./b",
            "https://gitlab.com/a%2fb",
            "https://gitlab.com/a\\b",
            "https://git lab.com",
            "https://github.com",
            "https://www.github.com",
            "https://api.github.com",
            "https://gitlab.com:99999",
            "",
        ):
            with self.subTest(typed=typed), self.assertRaises(ValueError):
                canonical_base_url(typed)


class TestParseProviderRepoUrl(BaseCase):

    def test_spellings_of_one_repository(self):
        expected = RepoRef(
            "gitlab", "https://gitlab.com", "gitlab.com", "acme/sub/addons",
            "https://gitlab.com/Acme/sub/addons.git",
        )
        for typed in (
            "https://gitlab.com/Acme/sub/addons",
            "https://gitlab.com/Acme/sub/addons.git",
            "https://gitlab.com/Acme/sub/addons/",
            "http://gitlab.com/Acme/sub/addons",
            "gitlab.com/Acme/sub/addons",
            "HTTPS://GITLAB.COM/Acme/sub/addons",
            "https://gitlab.com:443/Acme/sub/addons",
            "git@gitlab.com:Acme/sub/addons.git",
            "ssh://git@gitlab.com/Acme/sub/addons.git",
            "ssh://git@gitlab.com:2222/Acme/sub/addons.git",
        ):
            with self.subTest(typed=typed):
                self.assertEqual(parse_provider_repo_url(typed, _BASES), expected)

    def test_base_with_port_and_prefix(self):
        ref = parse_provider_repo_url(
            "https://git.acme.tld:8443/gitlab/team/addons", _BASES,
        )
        self.assertEqual(ref.authority, "git.acme.tld:8443")
        self.assertEqual(ref.path, "team/addons")
        self.assertEqual(
            ref.canonical, "https://git.acme.tld:8443/gitlab/team/addons.git",
        )
        # SSH matches on the host alone: its port is SSH's.
        self.assertEqual(
            parse_provider_repo_url(
                "git@git.acme.tld:gitlab/team/addons.git", _BASES,
            ),
            ref,
        )

    def test_authority_is_exact(self):
        for typed in (
            "https://gitlab.com.evil.tld/acme/addons",
            "https://evil.tld/gitlab.com/acme/addons",
            "https://evilgitlab.com/acme/addons",
            "https://gitlab.com:8443/acme/addons",
            # Missing or wrong prefix.
            "https://git.acme.tld:8443/team/addons",
            "https://git.acme.tld:8443/gitlabx/team/addons",
            "https://git.acme.tld/gitlab/team/addons",
        ):
            with self.subTest(typed=typed):
                self.assertIsNone(parse_provider_repo_url(typed, _BASES))

    def test_adversarial_values(self):
        for typed in (
            "https://tok@gitlab.com/acme/addons",
            "https://oauth2:tok@gitlab.com/acme/addons",
            "https://gitlab.com/acme/addons?x=1",
            "https://gitlab.com/acme/addons#main",
            "https://gitlab.com/acme/add%6fns",
            "https://gitlab.com/acme\\addons",
            "https://gitlab.com/acme/addons\tpin",
            "https://gitlab.com/acme/addons\nevil",
            "https://gitlab.com/acme/add ons",
            "https://gitlab.com/acme/addons\x00",
            "https://gitlab.com/acme/../addons",
            "https://gitlab.com/acme/a..b",
            "https://gitlab.com/acme/addons.atom",
            "https://gitlab.com/acme/-addons",
            "https://gitlab.com/acme/.addons",
            "https://gitlab.com/acme",
            "https://gitlab.com/" + "/".join(["g"] * 22),
            "ftp://gitlab.com/acme/addons",
            "ssh://root@gitlab.com/acme/addons",
            "root@gitlab.com:acme/addons",
            "https://gitlåb.com/acme/addons",
            "",
            None,
        ):
            with self.subTest(typed=typed):
                self.assertIsNone(parse_provider_repo_url(typed, _BASES))

    def test_twenty_one_segments_accepted(self):
        path = "/".join(f"g{i}" for i in range(21))
        ref = parse_provider_repo_url(f"https://gitlab.com/{path}", _BASES)
        self.assertEqual(ref.path, path)

    def test_no_bases(self):
        self.assertIsNone(
            parse_provider_repo_url("https://gitlab.com/acme/addons", ()),
        )


class TestCloneUrl(BaseCase):

    def setUp(self):
        super().setUp()
        self.ref = parse_provider_repo_url(
            "https://gitlab.com/acme/addons", _BASES,
        )

    def test_deploy_token_plus_is_not_encoded(self):
        self.assertEqual(
            clone_url_with_credential(
                self.ref, ("gitlab+deploy-token-42", "gldt-AbC_123.x"),
            ),
            "https://gitlab+deploy-token-42:gldt-AbC_123.x@gitlab.com/acme/addons.git",
        )

    def test_always_dot_git(self):
        self.assertTrue(clone_url_with_credential(self.ref, None).endswith(".git"))
        self.assertTrue(
            clone_url_with_credential(self.ref, ("oauth2", "glpat-abcdefgh"))
            .endswith("/acme/addons.git"),
        )

    def test_unsafe_credentials_refused(self):
        for cred in (
            ("oauth2", "short"),
            ("oauth2", "tok@evil.example"),
            ("oauth2", "tok:en:abc"),
            ("oauth2", "token/with/slash"),
            ("oauth 2", "glpat-abcdefgh"),
            ("", "glpat-abcdefgh"),
        ):
            with self.subTest(cred=cred), self.assertRaises(ValueError):
                clone_url_with_credential(self.ref, cred)


class TestNamespaces(BaseCase):

    def test_covers_by_segments(self):
        self.assertTrue(namespace_covers("", "acme/addons"))
        self.assertTrue(namespace_covers("acme", "acme/addons"))
        self.assertTrue(namespace_covers("acme", "acme/sub/addons"))
        self.assertTrue(namespace_covers("acme/sub", "acme/sub/addons"))
        self.assertTrue(namespace_covers("acme/addons", "acme/addons"))
        self.assertFalse(namespace_covers("acme", "acme-other/addons"))
        self.assertFalse(namespace_covers("acme/sub", "acme/subx/addons"))
        self.assertFalse(namespace_covers("acme/sub/x", "acme/sub"))

    def test_normalize(self):
        self.assertEqual(normalize_namespace(" /Acme/Sub/ "), "acme/sub")
        self.assertEqual(normalize_namespace(""), "")
        for bad in ("acme/../x", "acme/-x", "ac me", "a//b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_namespace(bad)
