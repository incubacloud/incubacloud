"""
Tier 1 & 2 — Tests for deploy_instance_executor helpers:
  - _github_authed_url (Tier 1)
  - _get_token_for_repo (Tier 1, mocked HTTP)
  - _build_answers domain format (Tier 2)
  - _repos_yaml_content (Tier 2)
"""
import unittest
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase


# ── Tier 1: _github_authed_url ───────────────────────────────────────────────

class TestGithubAuthedUrl(unittest.TestCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            _github_authed_url,
        )
        self.fn = _github_authed_url

    def test_ssh_converted_to_https(self):
        url = self.fn('git@github.com:OCA/web.git', None)
        self.assertEqual(url, 'https://github.com/OCA/web.git')

    def test_ssh_with_token(self):
        url = self.fn('git@github.com:OCA/web.git', 'tok123')
        self.assertEqual(
            url, 'https://x-access-token:tok123@github.com/OCA/web.git',
        )

    def test_bare_domain_gets_https(self):
        url = self.fn('github.com/org/repo.git', None)
        self.assertEqual(url, 'https://github.com/org/repo.git')

    def test_https_url_unchanged_without_token(self):
        url = self.fn('https://github.com/org/repo.git', None)
        self.assertEqual(url, 'https://github.com/org/repo.git')

    def test_https_url_with_token(self):
        url = self.fn('https://github.com/org/repo.git', 'mytoken')
        self.assertIn('x-access-token:mytoken@', url)

    def test_empty_url_returns_empty(self):
        self.assertFalse(self.fn('', 'tok'))
        self.assertFalse(self.fn(None, 'tok'))

    def test_non_github_url_no_token_injection(self):
        url = self.fn('https://gitlab.com/org/repo.git', 'tok')
        self.assertNotIn('x-access-token', url)

    def test_strips_whitespace(self):
        url = self.fn('  git@github.com:OCA/web.git  ', 'tok')
        self.assertIn('github.com', url)


# ── Tier 1: _get_token_for_repo (mocked HTTP) ───────────────────────────────

class TestGetTokenForRepo(unittest.TestCase):
    """Test token resolution per-repo with mocked GitHub API."""

    def _make_executor(self):
        """Create a minimal executor instance with mocked env."""
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )
        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        return executor

    @patch('urllib.request.urlopen')
    def test_app_token_preferred_when_both_work(self, mock_urlopen):
        mock_urlopen.return_value.__enter__ = lambda s: s
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        executor = self._make_executor()
        cache = {}
        result = executor._get_token_for_repo(
            'https://github.com/OCA/web.git',
            'app_tok', 'pat_tok', cache,
        )
        self.assertEqual(result, 'app_tok')

    @patch('urllib.request.urlopen')
    def test_pat_fallback_when_app_fails(self, mock_urlopen):
        import urllib.error
        # First call (app_tok) raises 404, second (pat_tok) succeeds
        mock_urlopen.side_effect = [
            urllib.error.HTTPError(None, 404, 'Not Found', {}, None),
            MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False)),
        ]
        executor = self._make_executor()
        cache = {}
        result = executor._get_token_for_repo(
            'https://github.com/OCA/web.git',
            'app_tok', 'pat_tok', cache,
        )
        self.assertEqual(result, 'pat_tok')

    def test_cache_hit_avoids_api_call(self):
        executor = self._make_executor()
        cache = {'OCA': 'cached_tok'}
        result = executor._get_token_for_repo(
            'https://github.com/OCA/web.git',
            'app_tok', 'pat_tok', cache,
        )
        self.assertEqual(result, 'cached_tok')

    def test_only_pat_when_no_app_token(self):
        executor = self._make_executor()
        cache = {}
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            result = executor._get_token_for_repo(
                'https://github.com/OCA/web.git',
                None, 'pat_tok', cache,
            )
        self.assertEqual(result, 'pat_tok')

    def test_empty_url_returns_none(self):
        executor = self._make_executor()
        cache = {}
        result = executor._get_token_for_repo('', 'a', 'b', cache)
        self.assertIsNone(result)

    def test_non_github_url_returns_best_effort(self):
        """URLs that can't be parsed fall back to app_token or pat."""
        executor = self._make_executor()
        cache = {}
        result = executor._get_token_for_repo(
            'https://gitlab.com/group/repo.git',
            'app_tok', 'pat_tok', cache,
        )
        self.assertEqual(result, 'app_tok')


# ── Tier 2: _build_answers domain format ─────────────────────────────────────

class TestBuildAnswersDomains(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'DomTest'})

    def _create_instance(self, env='production', domains=None):
        inst = self.env['cloud.instance'].create({
            'name': 'dom-test',
            'project_id': self.project.id,
            'environment': env,
            'domain_ids': domains or [],
        })
        return inst

    def _make_executor(self, inst):
        """Create executor with minimal mocking for _build_answers."""
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )
        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        executor.env = self.env
        job = MagicMock()
        job.instance_id = inst
        executor.job = job
        return executor

    def test_production_domains_in_domains_prod(self):
        inst = self._create_instance(env='production', domains=[
            (0, 0, {'hostname': 'app.example.com'}),
            (0, 0, {'hostname': 'www.example.com'}),
        ])
        answers = self._make_executor(inst)._build_answers()
        self.assertEqual(len(answers['domains_prod']), 2)
        self.assertEqual(answers['domains_test'], [])
        self.assertEqual(
            answers['domains_prod'][0],
            {'hosts': ['app.example.com']},
        )

    def test_staging_domains_in_domains_test(self):
        inst = self._create_instance(env='staging', domains=[
            (0, 0, {'hostname': 'staging.example.com'}),
        ])
        answers = self._make_executor(inst)._build_answers()
        self.assertEqual(len(answers['domains_test']), 1)
        self.assertEqual(answers['domains_prod'], [])

    def test_redirect_to_included(self):
        inst = self._create_instance(domains=[
            (0, 0, {
                'hostname': 'example.com',
                'redirect_to': 'www.example.com',
            }),
        ])
        answers = self._make_executor(inst)._build_answers()
        entry = answers['domains_prod'][0]
        self.assertEqual(entry['redirect_to'], 'www.example.com')

    def test_hostname_strips_protocol(self):
        inst = self._create_instance(domains=[
            (0, 0, {'hostname': 'https://app.example.com/'}),
        ])
        answers = self._make_executor(inst)._build_answers()
        self.assertEqual(
            answers['domains_prod'][0]['hosts'],
            ['app.example.com'],
        )

    def test_empty_domains_produces_empty_lists(self):
        inst = self._create_instance(domains=[])
        answers = self._make_executor(inst)._build_answers()
        self.assertEqual(answers['domains_prod'], [])
        self.assertEqual(answers['domains_test'], [])
