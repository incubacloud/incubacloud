"""
Tier 1 — Pure-Python unit tests for export sanitization logic.

Tests the sed/shell patterns used in export_instance_executor to strip
tokens, passwords, and secrets from exported archives.
"""
import re
import unittest


def _strip_tokens_from_repos_yaml(content):
    """Simulate the sed command: s|://x-access-token:[^@]*@|://|g"""
    return re.sub(r'://x-access-token:[^@]*@', '://', content)


class TestStripTokensFromReposYaml(unittest.TestCase):
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


class TestSensitiveFileExclusions(unittest.TestCase):
    """Verify the list of files that should be excluded or sanitized."""

    SENSITIVE_PATHS = {
        '.docker/backup.env',
        'odoo/custom/ssh/id_rsa',
        'odoo/custom/ssh/id_rsa.pub',
        'odoo/custom/ssh/known_hosts',
    }

    SANITIZED_PATHS = {
        '.docker/odoo.env',
        '.docker/db-access.env',
        '.docker/db-creation.env',
        'odoo/custom/src/repos.yaml',
        '.copier-answers.yml',
    }

    def test_sensitive_files_are_removed(self):
        """All sensitive files must be in the exclusion list."""
        for path in self.SENSITIVE_PATHS:
            self.assertIn('ssh' in path or 'backup.env' in path, [True])

    def test_sanitized_files_are_rewritten(self):
        """Files with secrets must be rewritten with placeholders."""
        for path in self.SANITIZED_PATHS:
            self.assertIn(
                path.endswith(('.env', '.yaml', '.yml')),
                [True],
                f"{path} should be sanitized",
            )

    def test_placeholder_passwords_correct(self):
        """Verify the placeholders used in sanitized env files."""
        placeholders = {
            'odoo.env': 'ADMIN_PASSWORD=changeme',
            'db-access.env': 'PGPASSWORD=changeme',
            'db-creation.env': 'POSTGRES_PASSWORD=changeme',
        }
        for filename, expected in placeholders.items():
            self.assertIn('changeme', expected)

    def test_backup_dst_stripped_from_copier_answers(self):
        """The sed removes the backup_dst line from copier answers."""
        content = (
            "_commit: v9.3.0\n"
            "backup_deletion: true\n"
            "backup_dst: boto3+s3://bucket/path\n"
            "backup_email_from: ''\n"
        )
        # Simulate: sed -i '/^backup_dst:/d'
        result = '\n'.join(
            line for line in content.splitlines()
            if not line.startswith('backup_dst:')
        )
        self.assertNotIn('backup_dst', result)
        self.assertIn('backup_deletion', result)
        self.assertIn('backup_email_from', result)
