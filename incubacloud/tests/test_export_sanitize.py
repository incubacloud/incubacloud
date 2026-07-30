"""
Tier 1 — the token-stripping contract of the instance export.

Since Phase 3 the sanitization itself lives in
``scripts/export_sanitize.sh`` and is tested end to end (real rsync,
real sed, real tar) in ``tests/shell/export_sanitize.bats``. What stays
here is the regex contract: which repository URLs must lose their
credentials and which must be left untouched.
"""
import re

from odoo.tests.common import BaseCase



def _strip_tokens_from_repos_yaml(content):
    """Simulate the sed command: s|://x-access-token:[^@]*@|://|g"""
    return re.sub(r'://x-access-token:[^@]*@', '://', content)


class TestStripTokensFromReposYaml(BaseCase):
    """Test the regex that strips tokens from repos.yaml URLs."""

    def test_strip_pat_token(self):
        line = (
            "    teproelec: https://x-access-token:"
            "fake_PAT_TOKEN_00000000000000000000000000"
            "@github.com/aquarian-tech/teproelec.git"
        )
        result = _strip_tokens_from_repos_yaml(line)
        self.assertNotIn('fake_PAT', result)
        self.assertIn('https://github.com/aquarian-tech/teproelec.git', result)

    def test_strip_app_token(self):
        line = (
            "    web: https://x-access-token:"
            "fake_APP_TOKEN_00000000000000000000000000"
            "@github.com/OCA/web.git"
        )
        result = _strip_tokens_from_repos_yaml(line)
        self.assertNotIn('fake_APP', result)
        self.assertIn('https://github.com/OCA/web.git', result)

    def test_multiple_tokens_in_one_file(self):
        content = (
            "    a: https://x-access-token:tok1@github.com/a/a.git\n"
            "    b: https://x-access-token:tok2@github.com/b/b.git\n"
        )
        result = _strip_tokens_from_repos_yaml(content)
        self.assertNotIn('tok1', result)
        self.assertNotIn('tok2', result)
        self.assertEqual(result.count('https://github.com/'), 2)

    def test_url_without_token_unchanged(self):
        line = "    odoo: https://github.com/odoo/odoo.git"
        result = _strip_tokens_from_repos_yaml(line)
        self.assertEqual(result, line)

    def test_non_url_content_unchanged(self):
        line = "  defaults:\n    depth: $DEPTH_DEFAULT"
        result = _strip_tokens_from_repos_yaml(line)
        self.assertEqual(result, line)
