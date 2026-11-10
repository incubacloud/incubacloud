"""
Tier 1 & 2 — Tests for deploy_instance_executor helpers:
  - _github_authed_url (Tier 1)
  - _get_token_for_repo (Tier 1, mocked HTTP)
  - _build_answers domain format (Tier 2)
  - _repos_yaml_content (Tier 2)
"""

import http.client
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml
from odoo.tests.common import BaseCase, TransactionCase

from ._certs import make_pair

# ── Tier 1: _github_authed_url ───────────────────────────────────────────────


class TestGithubAuthedUrl(BaseCase):
    def setUp(self):
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            _github_authed_url,
        )

        self.fn = _github_authed_url

    def test_ssh_converted_to_https(self):
        url = self.fn("git@github.com:OCA/web.git", None)
        self.assertEqual(url, "https://github.com/OCA/web.git")

    def test_ssh_with_token(self):
        url = self.fn("git@github.com:OCA/web.git", "tok123")
        self.assertEqual(
            url,
            "https://x-access-token:tok123@github.com/OCA/web.git",
        )

    def test_bare_domain_gets_https(self):
        url = self.fn("github.com/org/repo.git", None)
        self.assertEqual(url, "https://github.com/org/repo.git")

    def test_https_url_unchanged_without_token(self):
        url = self.fn("https://github.com/org/repo.git", None)
        self.assertEqual(url, "https://github.com/org/repo.git")

    def test_https_url_with_token(self):
        url = self.fn("https://github.com/org/repo.git", "mytoken")
        self.assertIn("x-access-token:mytoken@", url)

    def test_empty_url_returns_empty(self):
        self.assertFalse(self.fn("", "tok"))
        self.assertFalse(self.fn(None, "tok"))

    def test_non_github_url_no_token_injection(self):
        url = self.fn("https://gitlab.com/org/repo.git", "tok")
        self.assertNotIn("x-access-token", url)

    def test_strips_whitespace(self):
        url = self.fn("  git@github.com:OCA/web.git  ", "tok")
        self.assertIn("github.com", url)

    def test_token_only_for_the_github_com_host_itself(self):
        """A URL that merely contains ``github.com`` gets no token.

        ``"github.com" in url`` gave the token to
        ``https://evil.example/github.com/x.git`` (5-nov-2026): the
        host has to be ``github.com`` exactly, over https, with no
        user or port of its own.
        """
        for url in (
            "https://evil.example/github.com/x.git",
            "https://evil.example/?github.com",
            "https://github.com.evil.example/org/repo.git",
            "https://evil.example#github.com/org/repo",
            "https://user@github.com/org/repo.git",
            "https://github.com@evil.example/org/repo.git",
            "https://github.com:8443/org/repo.git",
            "ftp://github.com/org/repo.git",
            "https://github.com/org/repo.git?x=1",
            "https://github.com/org/repo\nhttps://evil.example/a",
        ):
            with self.subTest(url=url):
                self.assertNotIn("tok", self.fn(url, "tok"))

    def test_github_spellings_get_the_token_on_the_canonical_url(self):
        """Every spelling of a github.com repository is cloned from
        ``https://github.com/<owner>/<repo>``, with the token."""
        for url, expected in (
            ("http://github.com/org/repo.git", "org/repo.git"),
            ("www.github.com/org/repo", "org/repo"),
            ("https://GitHub.com/Org/Repo/", "Org/Repo"),
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    self.fn(url, "tok"),
                    f"https://x-access-token:tok@github.com/{expected}",
                )


# ── Tier 1: _get_token_for_repo (mocked HTTP) ───────────────────────────────


def _answer(repo_id=101, private=True):
    """GitHub's answer to ``GET /repos/{owner}/{repo}``, as ``safe_urlopen`` opens it.

    :rtype: MagicMock
    """
    body = json.dumps({"id": repo_id, "private": private}).encode()
    resp = MagicMock(spec=http.client.HTTPResponse)
    resp.__enter__.return_value = resp
    resp.read.return_value = body
    return resp


class TestGetTokenForRepo(BaseCase):
    """Test token resolution per-repo with mocked GitHub API."""

    def _make_executor(self):
        """Create a minimal executor instance with mocked env."""
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        return executor

    # Production code reaches GitHub via ``safe_urlopen`` (a no-redirect
    # opener defined in ``incubacloud.github.http_utils``), not directly
    # via ``urllib.request.urlopen``. Patching the latter is a no-op
    # here, so we patch the helper at its import site in the executor.
    _PATCH_PATH = "odoo.addons.incubacloud.models.deploy_instance_executor.safe_urlopen"

    @patch(_PATCH_PATH)
    def test_app_token_preferred_when_both_work(self, mock_urlopen):
        mock_urlopen.return_value = _answer()
        executor = self._make_executor()
        cache = {}
        result = executor._get_token_for_repo(
            "https://github.com/OCA/web.git",
            "app_tok",
            "pat_tok",
            cache,
        )
        self.assertEqual(result, "app_tok")

    @patch(_PATCH_PATH)
    def test_pat_fallback_when_app_fails(self, mock_urlopen):
        import urllib.error

        # First call (app_tok) raises 404, second (pat_tok) succeeds
        mock_urlopen.side_effect = [
            urllib.error.HTTPError(None, 404, "Not Found", {}, None),
            _answer(),
        ]
        executor = self._make_executor()
        cache = {}
        result = executor._get_token_for_repo(
            "https://github.com/OCA/web.git",
            "app_tok",
            "pat_tok",
            cache,
        )
        self.assertEqual(result, "pat_tok")

    @patch(_PATCH_PATH)
    def test_the_answer_is_kept_per_repository_not_per_owner(self, mock_urlopen):
        """Two repositories of one owner are each asked: an installation
        does not hold all of an owner's (10-nov-2026)."""
        mock_urlopen.side_effect = [_answer(1), _answer(2)]
        executor = self._make_executor()
        cache = {}
        for url in ("https://github.com/OCA/web.git", "https://github.com/OCA/web.git",
                    "https://github.com/OCA/server-ux"):
            executor._get_token_for_repo(url, "app_tok", None, cache)
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertEqual(cache["oca/web"], (1, True, "app"))
        self.assertEqual(cache["oca/server-ux"], (2, True, "app"))

    @patch(_PATCH_PATH)
    def test_no_token_when_neither_reads_the_repository(self, mock_urlopen):
        """No token on the chance it needs one: it used to be the App's."""
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(None, 404, "Not Found", {}, None)
        executor = self._make_executor()
        self.assertIsNone(executor._get_token_for_repo(
            "https://github.com/acme/secret.git", "app_tok", "pat_tok", {},
        ))
        self.assertEqual(mock_urlopen.call_count, 2)

    def _ask(self, side_effect, app="app_tok", pat="pat_tok"):
        """Ask for ``acme/secret`` with GitHub answering *side_effect*.

        :param list side_effect: what each ``safe_urlopen`` call does
        :return: the outcome (the token, or the exception raised) and the
            ``safe_urlopen`` mock
        :rtype: tuple
        """
        from odoo.addons.incubacloud.models import deploy_instance_executor as mod

        executor = self._make_executor()
        with patch(self._PATCH_PATH) as mock_urlopen, \
                patch.object(mod, "_REPO_ACCESS_RETRY_SECONDS", 0):
            mock_urlopen.side_effect = side_effect
            try:
                outcome = executor._get_token_for_repo(
                    "https://github.com/acme/secret.git", app, pat, {},
                )
            except Exception as exc:  # noqa: BLE001 — the outcome under test
                outcome = exc
        return outcome, mock_urlopen

    def test_a_failure_of_the_api_is_asked_again_once(self):
        """A 500 then GitHub's answer: the App's token (10-nov-2026)."""
        import urllib.error

        outcome, mock_urlopen = self._ask([
            urllib.error.HTTPError(None, 500, "Server Error", {}, None),
            _answer(),
        ])
        self.assertEqual(outcome, "app_tok")
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_a_failure_of_the_api_twice_refuses_the_job(self):
        """A 500, a timeout, a rate limit or an answer without an id twice
        is not «nobody reads it»: the job is refused with the reason,
        not run without credentials, and the PAT is not asked instead
        of the App (10-nov-2026)."""
        import urllib.error

        from odoo.addons.incubacloud.models.abstract_executor import JobRefused

        limited = {"X-RateLimit-Remaining": "0"}
        for name, failure in (
            ("500", lambda: urllib.error.HTTPError(None, 500, "Server Error", {}, None)),
            ("timeout", lambda: TimeoutError("timed out")),
            ("url", lambda: urllib.error.URLError("unreachable")),
            ("403 limit", lambda: urllib.error.HTTPError(None, 403, "Forbidden", limited, None)),
            ("429", lambda: urllib.error.HTTPError(None, 429, "Too Many", {}, None)),
            ("no id", lambda: _answer(repo_id="x")),
        ):
            with self.subTest(failure=name):
                side = [failure(), failure()]
                outcome, mock_urlopen = self._ask(side)
                self.assertIsInstance(outcome, JobRefused)
                self.assertIn("acme/secret", str(outcome))
                self.assertEqual(mock_urlopen.call_count, 2)

    def test_401_and_403_without_a_rate_limit_mean_the_token_does_not_read_it(self):
        """A 401 or a plain 403 of the App goes on to the PAT, unasked again."""
        import urllib.error

        for code in (401, 403):
            with self.subTest(code=code):
                outcome, mock_urlopen = self._ask([
                    urllib.error.HTTPError(None, code, "No", {}, None),
                    _answer(),
                ])
                self.assertEqual(outcome, "pat_tok")
                self.assertEqual(mock_urlopen.call_count, 2)

    def test_only_pat_when_no_app_token(self):
        executor = self._make_executor()
        cache = {}
        with patch(self._PATCH_PATH) as mock_urlopen:
            mock_urlopen.return_value = _answer()
            result = executor._get_token_for_repo(
                "https://github.com/OCA/web.git",
                None,
                "pat_tok",
                cache,
            )
        self.assertEqual(result, "pat_tok")

    def test_empty_url_returns_none(self):
        executor = self._make_executor()
        cache = {}
        result = executor._get_token_for_repo("", "a", "b", cache)
        self.assertIsNone(result)

    @patch(_PATCH_PATH)
    def test_a_url_of_another_host_gets_no_token(self, mock_urlopen):
        """A URL that is not a github.com repository gets no token at all.

        It used to get the App's token or the PAT on the chance it
        needed one, and ``_github_authed_url`` put it in the URL
        (5-nov-2026).
        """
        executor = self._make_executor()
        for url in (
            "https://gitlab.com/group/repo.git",
            "https://evil.example/github.com/x.git",
            "https://github.com.evil.example/org/repo.git",
            "https://github.com:8443/org/repo.git",
        ):
            with self.subTest(url=url):
                cache = {}
                self.assertIsNone(
                    executor._get_token_for_repo(url, "app_tok", "pat_tok", cache),
                )
                self.assertEqual(cache, {})
        mock_urlopen.assert_not_called()

    @patch(_PATCH_PATH)
    def test_an_ssh_spelled_github_repo_still_gets_its_token(self, mock_urlopen):
        """``git@github.com:`` is how a deployment may spell its own repo."""
        mock_urlopen.return_value = _answer()
        executor = self._make_executor()
        self.assertEqual(
            executor._get_token_for_repo(
                "git@github.com:acme/addons.git", "app_tok", "pat_tok", {},
            ),
            "app_tok",
        )

    def test_a_renamed_or_moved_repository_refuses_the_job_at_once(self):
        """GitHub answers a renamed or transferred repository with a
        redirect, which the panel does not follow: the job is refused
        naming the repository, without asking again and without the
        token."""
        import urllib.error

        from odoo.addons.incubacloud.models.abstract_executor import JobRefused

        moved = {"Location": "https://api.github.com/repositories/101"}
        for code in (301, 302, 307, 308):
            with self.subTest(code=code):
                outcome, mock_urlopen = self._ask([
                    urllib.error.HTTPError(None, code, "Moved", moved, None),
                    _answer(),
                ])
                self.assertIsInstance(outcome, JobRefused)
                self.assertIn("acme/secret", str(outcome))
                self.assertIn("renamed or moved", str(outcome))
                self.assertNotIn("app_tok", str(outcome))
                self.assertEqual(mock_urlopen.call_count, 1)


class TestAnAppThatIssuesNoToken(TransactionCase):
    """When the panel asks GitHub for its App's token, and what it does
    when GitHub does not issue it (10-nov-2026)."""

    def setUp(self):
        super().setUp()
        self.env["cloud.settings"]._get_system().github_pat = "pat_tok"

    def _tokens(self, client):
        """Call ``_get_github_tokens`` with *client* as the panel's App.

        :param client: the App's client, or None for a panel without one
        :return: the tokens, or the exception raised, and how often the
            App's token was asked
        :rtype: tuple
        """
        from odoo.addons.incubacloud.models import deploy_instance_executor as mod

        executor = mod.DeployInstanceExecutor.__new__(mod.DeployInstanceExecutor)
        executor.env = self.env
        with patch.object(
            mod.DeployInstanceExecutor, "_github_app_client", autospec=True,
            return_value=client,
        ), patch.object(mod, "_REPO_ACCESS_RETRY_SECONDS", 0):
            try:
                outcome = executor._get_github_tokens()
            except Exception as exc:  # noqa: BLE001 — the outcome under test
                outcome = exc
        asked = client.get_installation_token.call_count if client else 0
        return outcome, asked

    @staticmethod
    def _client(*answers):
        """An App client whose token requests answer *answers* in turn."""
        from odoo.addons.incubacloud.github.client import GitHubAppClient

        client = MagicMock(spec=GitHubAppClient)
        client.get_installation_token.side_effect = list(answers)
        return client

    def test_a_refusal_of_github_refuses_the_job_at_once(self):
        """A 4xx is the App's to fix in Settings; the PAT is not tried."""
        from odoo.addons.incubacloud.github.client import GitHubAPIError
        from odoo.addons.incubacloud.models.abstract_executor import JobRefused

        for code in (401, 404, 422):
            with self.subTest(code=code):
                outcome, asked = self._tokens(
                    self._client(GitHubAPIError(code, "No"), "app_tok"),
                )
                self.assertIsInstance(outcome, JobRefused)
                self.assertIn("Check the App in Settings", str(outcome))
                self.assertNotIn("pat_tok", str(outcome))
                self.assertEqual(asked, 1)

    def test_a_passing_failure_is_asked_again_once(self):
        """A 5xx, a timeout or the network, then the token: the job goes on."""
        import urllib.error

        from odoo.addons.incubacloud.github.client import GitHubAPIError

        for name, failure in (
            ("502", GitHubAPIError(502, "Bad Gateway")),
            ("timeout", TimeoutError("timed out")),
            ("network", urllib.error.URLError("unreachable")),
        ):
            with self.subTest(failure=name):
                outcome, asked = self._tokens(self._client(failure, "app_tok"))
                self.assertEqual(outcome, ("app_tok", "pat_tok"))
                self.assertEqual(asked, 2)

    def test_a_failure_twice_refuses_the_job_to_be_run_again(self):
        """Twice a failure of GitHub: the job is refused, and the message
        says GitHub did not issue the token, not to check the App."""
        from odoo.addons.incubacloud.github.client import GitHubAPIError
        from odoo.addons.incubacloud.models.abstract_executor import JobRefused

        outcome, asked = self._tokens(self._client(
            GitHubAPIError(503, "Unavailable"), TimeoutError("timed out"),
        ))
        self.assertIsInstance(outcome, JobRefused)
        self.assertIn("GitHub did not issue", str(outcome))
        self.assertIn("Run the job again", str(outcome))
        self.assertNotIn("Settings", str(outcome))
        self.assertNotIn("pat_tok", str(outcome))
        self.assertEqual(asked, 2)

    def test_a_panel_without_an_app_uses_the_pat(self):
        """No App configured is not a failure."""
        self.assertEqual(self._tokens(None), ((None, "pat_tok"), 0))

    def test_an_app_not_installed_yet_counts_as_none(self):
        """An App with no installation id yet: the PAT, as with no App."""
        from odoo.addons.incubacloud.github.client import (
            GitHubAPIError,
            GitHubAppClient,
        )
        from odoo.addons.incubacloud.github.credentials import GitHubAppCredentials
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        executor.env = self.env
        service = type(self.env["cloud.github.credential.service"])
        credentials = GitHubAppCredentials(
            app_id="1", installation_id="", private_key_pem="",
        )
        with patch.object(
            service, "get_credentials", autospec=True, return_value=credentials,
        ), patch.object(
            GitHubAppClient, "get_installation_token", autospec=True,
            side_effect=GitHubAPIError(404, "Not Found"),
        ) as issue:
            outcome = executor._get_github_tokens()
        self.assertEqual(outcome, (None, "pat_tok"))
        issue.assert_not_called()


class TestTheTokensAreAskedOnlyForGitHubLines(BaseCase):
    """An instance with no line of its own on github.com does not depend on
    the GitHub App: Odoo's is public, and a line of another provider uses
    its provider's credential (10-nov-2026)."""

    def _render(self, repos):
        """Render repos.yaml for *repos* with GitHub's tokens refused.

        :param list repos: the instance's lines
        :return: the rendered text, or the exception raised, and the mock
            of ``_get_github_tokens``
        :rtype: tuple
        """
        from odoo.addons.incubacloud.models.abstract_executor import JobRefused
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        from .test_executor_features import _make_deploy_executor

        ex = _make_deploy_executor(
            odoo_version="18.0", odoo_commit_sha=False, repo_ids=repos,
        )
        with patch.object(
            DeployInstanceExecutor, "_get_github_tokens", autospec=True,
            side_effect=JobRefused("Refused: no App token"),
        ) as tokens, patch.object(
            DeployInstanceExecutor, "_gitlab_clone_url", autospec=True,
            side_effect=lambda _self, url, _provider: url,
        ):
            try:
                outcome = ex._repos_yaml_content(set())
            except Exception as exc:  # noqa: BLE001 — the outcome under test
                outcome = exc
        return outcome, tokens

    def test_only_odoo_asks_no_token(self):
        """Odoo's line alone renders without GitHub's tokens."""
        outcome, tokens = self._render([])
        self.assertIn("odoo", yaml.safe_load(outcome))
        tokens.assert_not_called()

    def test_a_line_of_another_provider_asks_no_github_token(self):
        """A GitLab line renders with its provider's URL, no GitHub token."""
        line = SimpleNamespace(
            url="https://gitlab.example.com/acme/addons.git", branch="main",
            commit_sha=False, provider_id=SimpleNamespace(id=3),
        )
        outcome, tokens = self._render([line])
        self.assertIn("addons", yaml.safe_load(outcome))
        tokens.assert_not_called()

    def test_a_github_line_still_needs_the_tokens(self):
        """A line of github.com asks them, and their refusal stops the job."""
        from odoo.addons.incubacloud.models.abstract_executor import JobRefused

        line = SimpleNamespace(
            url="https://github.com/acme/addons.git", branch="main",
            commit_sha=False, provider_id=False,
        )
        outcome, tokens = self._render([line])
        self.assertIsInstance(outcome, JobRefused)
        tokens.assert_called_once()


class TestTheTokenEachLineGets(BaseCase):
    """What a server is handed for each GitHub line (10-nov-2026).

    A private repository the App reads gets a token scoped to the
    instance's private repositories the App reads, asked once; one only
    the PAT reads, the PAT; a public one, nothing; one nobody reads,
    nothing either. The installation's own token never reaches the spec.
    """

    _SCOPED = "ghs_scopedTokenOfTheJob"
    _GENERAL = "ghs_installationToken"

    def _executor(self, repos, access, scoped=None):
        """Render repos.yaml for *repos* with GitHub answering *access*.

        :param list repos: URLs of the instance's lines
        :param dict access: ``{url: (repo_id, private, by)}``
        :param scoped: what the scoped token request does: a token, or an
            exception to raise
        :return: the rendered text, the scoped token mock and the log
        :rtype: tuple
        """
        from odoo.addons.incubacloud.github.client import GitHubAppClient
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        from .test_executor_features import _make_deploy_executor

        ex = _make_deploy_executor(
            odoo_version="18.0", odoo_commit_sha=False,
            repo_ids=[SimpleNamespace(url=u, branch="main", commit_sha=False)
                      for u in repos],
        )
        logged = []
        ex._sys = logged.append
        client = MagicMock(spec=GitHubAppClient)
        if isinstance(scoped, Exception):
            client.get_scoped_installation_token.side_effect = scoped
        else:
            client.get_scoped_installation_token.return_value = scoped or self._SCOPED
        ex._github_app_client = lambda: client
        with patch.object(
            DeployInstanceExecutor, "_get_github_tokens", autospec=True,
            return_value=(self._GENERAL, "pat_tok"),
        ), patch.object(
            DeployInstanceExecutor, "_github_repo_access", autospec=True,
            side_effect=lambda _self, url, *_a: access[url],
        ):
            text = ex._repos_yaml_content(set())
        return ex, text, client.get_scoped_installation_token, logged

    def _remote(self, text, alias):
        """The URL of *alias* in the rendered *text*."""
        return next(iter(yaml.safe_load(text)[alias]["remotes"].values()))

    def test_the_scoped_token_is_asked_with_the_private_repositories_the_app_reads(self):
        """One request, with the ids of the private repositories the App reads."""
        access = {
            "https://github.com/acme/a": (11, True, "app"),
            "https://github.com/acme/b": (12, True, "app"),
            "https://github.com/acme/pub": (13, False, "app"),
            "https://github.com/acme/pat-only": (14, True, "pat"),
        }
        _ex, text, scoped, _log = self._executor(list(access), access)
        scoped.assert_called_once_with([11, 12])
        self.assertIn(f"x-access-token:{self._SCOPED}@", self._remote(text, "a"))
        self.assertIn(f"x-access-token:{self._SCOPED}@", self._remote(text, "b"))
        self.assertNotIn(self._GENERAL, text)

    def test_a_public_repository_gets_no_credential_even_if_the_app_reads_it(self):
        """Nothing for a public repository, whoever reads it."""
        access = {"https://github.com/acme/pub": (13, False, "app")}
        _ex, text, scoped, _log = self._executor(list(access), access)
        self.assertEqual(self._remote(text, "pub"), "https://github.com/acme/pub")
        scoped.assert_not_called()

    def test_one_nobody_reads_gets_no_credential_and_the_log_says_so(self):
        """No token handed out on the chance it needs one."""
        access = {"https://github.com/acme/secret": (None, None, None)}
        _ex, text, _scoped, logged = self._executor(list(access), access)
        self.assertEqual(self._remote(text, "secret"), "https://github.com/acme/secret")
        self.assertNotIn("pat_tok", text)
        self.assertNotIn(self._GENERAL, text)
        self.assertTrue(any("neither the GitHub App" in line for line in logged))

    def test_one_only_the_pat_reads_gets_the_pat(self):
        """The PAT for what only it reads, and no scoped token asked."""
        access = {"https://github.com/acme/pat-only": (14, True, "pat")}
        _ex, text, scoped, _log = self._executor(list(access), access)
        self.assertIn("x-access-token:pat_tok@", self._remote(text, "pat-only"))
        scoped.assert_not_called()

    def test_a_refused_scoped_token_refuses_the_job_and_no_token_reaches_the_spec(self):
        """The installation's own token is never used instead."""
        from odoo.addons.incubacloud.github.client import GitHubAPIError
        from odoo.addons.incubacloud.models.abstract_executor import JobRefused

        access = {"https://github.com/acme/a": (11, True, "app")}
        with self.assertRaises(JobRefused) as caught:
            self._executor(list(access), access, scoped=GitHubAPIError(422, "no"))
        self.assertIn("installation's own token is not used", str(caught.exception))
        self.assertNotIn(self._GENERAL, str(caught.exception))


# ── Tier 2: _build_answers domain format ─────────────────────────────────────


class TestBuildAnswersDomains(TransactionCase):
    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "DomTest"})

    def _create_instance(self, env="production", domains=None):
        inst = self.env["cloud.instance"].create(
            {
                "name": "dom-test",
                "project_id": self.project.id,
                "environment": env,
                "domain_ids": domains or [],
            }
        )
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
        inst = self._create_instance(
            env="production",
            domains=[
                (0, 0, {"hostname": "app.example.com"}),
                (0, 0, {"hostname": "www.example.com"}),
            ],
        )
        answers = self._make_executor(inst)._build_answers()
        self.assertEqual(len(answers["domains_prod"]), 2)
        self.assertEqual(answers["domains_test"], [])
        self.assertEqual(
            answers["domains_prod"][0],
            {"hosts": ["app.example.com"], "cert_resolver": "letsencrypt"},
        )

    def test_staging_domains_in_domains_test(self):
        inst = self._create_instance(
            env="staging",
            domains=[
                (0, 0, {"hostname": "staging.example.com"}),
            ],
        )
        answers = self._make_executor(inst)._build_answers()
        self.assertEqual(len(answers["domains_test"]), 1)
        self.assertEqual(answers["domains_prod"], [])

    def test_redirect_to_included(self):
        inst = self._create_instance(
            domains=[
                (
                    0,
                    0,
                    {
                        "hostname": "example.com",
                        "redirect_to": "www.example.com",
                    },
                ),
            ]
        )
        answers = self._make_executor(inst)._build_answers()
        entry = answers["domains_prod"][0]
        self.assertEqual(entry["redirect_to"], "www.example.com")

    def test_hostname_with_protocol_is_rejected_by_model(self):
        """The hostname format check on cloud.instance.domain rejects URL
        scheme/path before the executor ever sees the value, so the
        ``_build_answers`` strip helper only has to defend against
        whitespace. Pin that contract here."""
        from odoo.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self._create_instance(
                domains=[
                    (0, 0, {"hostname": "https://app.example.com/"}),
                ]
            )

    def test_empty_domains_produces_empty_lists(self):
        inst = self._create_instance(domains=[])
        answers = self._make_executor(inst)._build_answers()
        self.assertEqual(answers["domains_prod"], [])
        self.assertEqual(answers["domains_test"], [])

    # ── cert_resolver / redirect_permanent ───────────────────────────────
    #
    # The three options map onto the three shapes doodba's Traefik macro
    # accepts, and the mapping is the whole point of the feature: a wrong
    # value here silently changes an instance's TLS on the next rebuild.

    def _host(self, **vals):
        """Return a host an instance can be placed on."""
        base = {
            "name": "auto-tls-host",
            "ip_address": "198.51.100.77",
            "user": "root",
            "wildcard_domain": "example.com",
        }
        base.update(vals)
        return self.env["cloud.host"].create(base)

    def _entry_on_host(self, host, **domain_vals):
        """Return the copier entry for one domain served by *host*.

        A project of its own per call, because a project accepts only
        one production instance and these entries only exist on a
        production one. The name and hostname are unique for the same
        reason: a test asking twice should fail on what it asserts, not
        on the fixture.
        """
        self._seq = getattr(self, "_seq", 0) + 1
        vals = {"cert_resolver": "auto"} | domain_vals
        project = self.env["cloud.project"].create(
            {"name": f"DomTest-{self._seq}"},
        )
        inst = self.env["cloud.instance"].create({
            "name": f"dom-host-{self._seq}",
            "project_id": project.id,
            "environment": "production",
            "host_id": host.id,
            "domain_ids": [
                (0, 0, {"hostname": f"c{self._seq}.example.com"} | vals),
            ],
        })
        return self._make_executor(inst)._build_answers()["domains_prod"][0]

    def _entry_for(self, **domain_vals):
        """Return the single copier entry produced for one domain."""
        inst = self._create_instance(
            domains=[
                (0, 0, {"hostname": "c.example.com"} | domain_vals),
            ],
        )
        return self._make_executor(inst)._build_answers()["domains_prod"][0]

    def test_letsencrypt_emits_the_resolver_name(self):
        """A string makes the macro emit tls.certResolver — ACME issuance."""
        entry = self._entry_for(cert_resolver="letsencrypt")
        self.assertEqual(entry["cert_resolver"], "letsencrypt")

    def test_custom_emits_boolean_true(self):
        """``true`` means TLS on, no resolver: serve the host's own cert."""
        entry = self._entry_for(cert_resolver="custom")
        self.assertIs(entry["cert_resolver"], True)

    def test_none_emits_boolean_false(self):
        """``false`` leaves the router without any TLS configuration."""
        entry = self._entry_for(cert_resolver="none")
        self.assertIs(entry["cert_resolver"], False)

    def test_cert_resolver_is_always_emitted(self):
        """Never rely on the template default.

        The copier copy runs against a moving upstream ref, so a default
        that changes between template releases would rewrite every
        instance's TLS on its next rebuild with nothing to show for it.
        """
        self.assertIn("cert_resolver", self._entry_for())

    def test_auto_falls_back_to_a_ca_without_a_host(self):
        """A domain on an instance nobody has placed yet still deploys
        the way it used to."""
        entry = self._entry_for(cert_resolver="auto")
        self.assertEqual(entry["cert_resolver"], "letsencrypt")

    def test_auto_asks_a_ca_while_visitors_still_arrive_directly(self):
        entry = self._entry_on_host(self._host())
        self.assertEqual(entry["cert_resolver"], "letsencrypt")

    def test_auto_serves_the_held_certificate_once_a_cdn_answers(self):
        cert, key = make_pair(["example.com", "*.example.com"])
        entry = self._entry_on_host(self._host(
            behind_cdn=True, tls_default_cert=cert, tls_default_key=key,
        ))
        self.assertIs(entry["cert_resolver"], True)

    def test_auto_keeps_asking_for_a_name_the_certificate_misses(self):
        """A customer's own domain on a host behind our CDN: ours covers
        our zone and not theirs, so there is nothing to serve."""
        cert, key = make_pair(["other.test", "*.other.test"])
        entry = self._entry_on_host(self._host(
            behind_cdn=True, tls_default_cert=cert, tls_default_key=key,
        ))
        self.assertEqual(entry["cert_resolver"], "letsencrypt")

    def test_an_explicit_choice_is_not_overridden_by_the_host(self):
        """``none`` is how somebody turns TLS off on purpose."""
        cert, key = make_pair(["example.com", "*.example.com"])
        host = self._host(
            behind_cdn=True, tls_default_cert=cert, tls_default_key=key,
        )
        self.assertIs(
            self._entry_on_host(host, cert_resolver="none")["cert_resolver"],
            False,
        )
        self.assertEqual(
            self._entry_on_host(
                host, cert_resolver="letsencrypt",
            )["cert_resolver"],
            "letsencrypt",
        )

    def test_redirect_permanent_only_when_redirecting(self):
        """301 is meaningless without a redirect target — don't emit it."""
        self.assertNotIn(
            "redirect_permanent", self._entry_for(redirect_permanent=True),
        )

    def test_redirect_permanent_emitted_with_a_target(self):
        entry = self._entry_for(
            redirect_to="www.example.com", redirect_permanent=True,
        )
        self.assertIs(entry["redirect_permanent"], True)

    def test_temporary_redirect_omits_the_flag(self):
        """Absent means 302, the template default — no need to say it."""
        entry = self._entry_for(redirect_to="www.example.com")
        self.assertNotIn("redirect_permanent", entry)


# ── Tier 2: Backup policy hooks (_backup_enabled, _backup_retention) ─────────


class TestBackupHooksDefault(TransactionCase):
    """Default behaviour on the core deploy executor with no overrides."""

    def setUp(self):
        super().setUp()
        # Clear the global backup backend so tests run in isolation.
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id",
            "0",
        )
        self.project = self.env["cloud.project"].create({"name": "BackupHooks"})
        self.Backend = self.env["cloud.backup.backend"]

    def _make_executor(self, inst):
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        executor.env = self.env
        job = MagicMock()
        job.instance_id = inst
        executor.job = job
        return executor

    def _instance(self, backend=None):
        return self.env["cloud.instance"].create(
            {
                "name": "bk-test",
                "project_id": self.project.id,
                "environment": "production",
                "backup_backend_id": backend.id if backend else False,
            }
        )

    def test_backup_enabled_true_when_backend_with_dst(self):
        bb = self.Backend.create(
            {
                "name": "Full",
                "backend_type": "s3",
                "s3_bucket": "my-bucket",
            }
        )
        self.assertTrue(bb.backup_dst)  # sanity
        inst = self._instance(backend=bb)
        self.assertTrue(self._make_executor(inst)._backup_enabled())

    def test_backup_enabled_false_when_no_backend(self):
        inst = self._instance(backend=None)
        self.assertFalse(self._make_executor(inst)._backup_enabled())

    def test_backup_enabled_false_when_backend_without_dst(self):
        bb = self.Backend.create(
            {
                "name": "NoBucket",
                "backend_type": "s3",
            }
        )
        self.assertFalse(bb.backup_dst)  # no bucket → no dst
        inst = self._instance(backend=bb)
        self.assertFalse(self._make_executor(inst)._backup_enabled())

    def test_backup_retention_from_backend(self):
        bb = self.Backend.create(
            {
                "name": "Ret",
                "backend_type": "s3",
                "s3_bucket": "b",
                "backup_retention": "14D",
            }
        )
        inst = self._instance(backend=bb)
        self.assertEqual(
            self._make_executor(inst)._backup_retention(),
            "14D",
        )

    def test_backup_retention_fallback_3m_when_no_backend(self):
        inst = self._instance(backend=None)
        self.assertEqual(
            self._make_executor(inst)._backup_retention(),
            "3M",
        )

    def test_backup_env_content_none_when_disabled(self):
        inst = self._instance(backend=None)
        self.assertIsNone(self._make_executor(inst)._backup_env_content())

    def test_backup_env_content_includes_retention_override(self):
        bb = self.Backend.create(
            {
                "name": "E",
                "backend_type": "s3",
                "s3_bucket": "b",
                "backup_retention": "7D",
                "s3_access_key_id": "AK",
                "s3_secret_access_key": "SK",
            }
        )
        inst = self._instance(backend=bb)
        content = self._make_executor(inst)._backup_env_content()
        self.assertIn("JOB_800_WHAT", content)
        self.assertIn("7D", content)

    def test_build_answers_backup_deletion_true_when_retention_owner_cron(self):
        bb = self.Backend.create(
            {
                "name": "Cron",
                "backend_type": "s3",
                "s3_bucket": "b",
                "retention_owner": "cron",
            }
        )
        inst = self._instance(backend=bb)
        answers = self._make_executor(inst)._build_answers()
        self.assertTrue(answers["backup_deletion"])

    def test_build_answers_backup_deletion_false_when_retention_owner_lifecycle(self):
        bb = self.Backend.create(
            {
                "name": "Lifecycle",
                "backend_type": "s3",
                "s3_bucket": "b",
                "retention_owner": "lifecycle",
            }
        )
        inst = self._instance(backend=bb)
        answers = self._make_executor(inst)._build_answers()
        self.assertFalse(answers["backup_deletion"])

    def test_backup_env_content_skips_retention_when_default_3m(self):
        bb = self.Backend.create(
            {
                "name": "D",
                "backend_type": "s3",
                "s3_bucket": "b",
                "backup_retention": "3M",
            }
        )
        inst = self._instance(backend=bb)
        content = self._make_executor(inst)._backup_env_content()
        # Default 3M matches copier default → no override line
        self.assertNotIn("JOB_800_WHAT", content)


# ── Tier 2: TLS preflight (what a domain asks for vs what the host holds) ────


class TestTlsPreflight(TransactionCase):
    """Each domain's certificate choice is checked against what the host
    can give before anything reaches the host, and written to the job log
    so the decision ``auto`` took is visible without reading code."""

    def setUp(self):
        super().setUp()
        self._seq = 0

    def _host(self, **vals):
        """Return a host instances can be placed on."""
        self._seq += 1
        return self.env["cloud.host"].create({
            "name": f"tls-preflight-{self._seq}",
            "ip_address": f"198.51.100.{100 + self._seq}",
            "user": "root",
            "wildcard_domain": "example.com",
        } | vals)

    def _executor_with(self, host, executor_cls=None, **domain_vals):
        """Return an executor for one production domain on *host*.

        :param executor_cls: executor class to build, the deploy one by
            default
        """
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        executor_cls = executor_cls or DeployInstanceExecutor
        self._seq += 1
        project = self.env["cloud.project"].create(
            {"name": f"TlsPre-{self._seq}"},
        )
        inst = self.env["cloud.instance"].create({
            "name": f"tls-pre-{self._seq}",
            "project_id": project.id,
            "environment": "production",
            "host_id": host.id,
            "domain_ids": [
                (0, 0, {"hostname": "c.example.com"} | domain_vals),
            ],
        })
        executor = executor_cls.__new__(executor_cls)
        executor.env = self.env
        executor._log_buffer = []
        job = MagicMock(spec=type(self.env["cloud.job"]))
        job.instance_id = inst
        executor.job = job
        return executor

    def _log(self, executor):
        """Return the system lines the executor wrote."""
        return [msg for msg, _kind in executor._log_buffer]

    def test_custom_without_a_host_certificate_fails_before_shipping(self):
        executor = self._executor_with(self._host(), cert_resolver="custom")
        with self.assertRaisesRegex(RuntimeError, "c.example.com"):
            executor._preflight_tls_check()

    def test_custom_with_a_covering_certificate_passes(self):
        cert, key = make_pair(["c.example.com"])
        executor = self._executor_with(
            self._host(tls_default_cert=cert, tls_default_key=key),
            cert_resolver="custom",
        )
        executor._preflight_tls_check()
        self.assertIn("TLS for c.example.com: host's certificate",
                      self._log(executor))

    def test_custom_not_covering_the_name_warns_but_ships(self):
        """A wildcard or an internal CA can be right in ways the check
        cannot see, so a mismatch is said, not refused."""
        cert, key = make_pair(["other.example.com"])
        executor = self._executor_with(
            self._host(tls_default_cert=cert, tls_default_key=key),
            cert_resolver="custom",
        )
        executor._preflight_tls_check()
        self.assertTrue(
            any("does not cover" in line for line in self._log(executor)),
        )

    def test_custom_with_certificates_declared_in_config_yml_ships(self):
        """RB-12 loads certificates by hand and declares them in the
        host's ``config.yml``; the panel cannot read those files, so it
        trusts the declaration instead of refusing the deploy."""
        host = self._host()
        host.traefik_config_yml = (
            (host.traefik_config_yml or "")
            + "\ntls:\n  certificates:\n"
            "    - certFile: /etc/certs/fullchain.pem\n"
            "      keyFile: /etc/certs/privkey.pem\n"
        )
        executor = self._executor_with(host, cert_resolver="custom")
        executor._preflight_tls_check()
        self.assertIn(
            "TLS for c.example.com: a certificate declared in the host's "
            "config.yml (not checked)",
            self._log(executor),
        )

    def test_auto_names_the_host_certificate_when_a_cdn_answers(self):
        cert, key = make_pair(["c.example.com"])
        executor = self._executor_with(
            self._host(
                behind_cdn=True, tls_default_cert=cert, tls_default_key=key,
            ),
            cert_resolver="auto",
        )
        executor._preflight_tls_check()
        self.assertIn(
            "TLS for c.example.com: automatic → host's certificate "
            "(a CDN answers for it)",
            self._log(executor),
        )

    def test_auto_names_lets_encrypt_on_a_direct_host(self):
        executor = self._executor_with(self._host(), cert_resolver="auto")
        executor._preflight_tls_check()
        self.assertIn(
            "TLS for c.example.com: automatic → Let's Encrypt",
            self._log(executor),
        )

    def test_deploy_and_rebuild_check_before_touching_the_host(self):
        """Both entry points that ship copier answers run the check first,
        so a refused ``custom`` never reaches the upload."""
        import asyncio
        from unittest.mock import AsyncMock

        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )
        from odoo.addons.incubacloud.models.rebuild_instance_executor import (
            RebuildInstanceExecutor,
        )

        for cls in (DeployInstanceExecutor, RebuildInstanceExecutor):
            executor = self._executor_with(
                self._host(),
                executor_cls=cls,
                cert_resolver="custom",
                hostname=f"{cls.__name__.lower()}.example.com",
            )
            with patch.object(
                cls, "_upload_copier_files", new_callable=AsyncMock,
            ) as upload:
                with self.assertRaisesRegex(RuntimeError, "holds none"):
                    asyncio.run(executor.before_execute(transport=None))
                upload.assert_not_called()
