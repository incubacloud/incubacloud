"""
Tier 1 — Pure-Python unit tests for standalone utility functions:
  - _redact_tokens  (abstract_executor)
  - _slugify        (cloud_project)
  - ExecutorRegistry (registry)
  - _normalize_domain / _parse_github_repo_path (data_load controller)
"""
import unittest

from odoo.tests.common import BaseCase



# ── _redact_tokens ────────────────────────────────────────────────────────────

class TestRedactTokens(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.abstract_executor import _redact_tokens
        self.redact = _redact_tokens

    def test_removes_token_from_https_clone_url(self):
        line = "git clone https://x-access-token:ghs_abc123@github.com/owner/repo.git"
        result = self.redact(line)
        self.assertNotIn("ghs_abc123", result)
        self.assertIn("https://github.com", result)

    def test_removes_pat_from_https_remote(self):
        line = "origin https://ghp_longpersonalaccesstoken@github.com/org/repo"
        result = self.redact(line)
        self.assertNotIn("ghp_longpersonalaccesstoken", result)
        self.assertIn("github.com", result)

    def test_plain_text_unchanged(self):
        line = "Running: docker compose up -d"
        self.assertEqual(self.redact(line), line)

    def test_url_without_credentials_unchanged(self):
        line = "https://github.com/owner/repo.git"
        self.assertEqual(self.redact(line), line)

    def test_multiple_credentials_in_one_line(self):
        line = (
            "Cloning https://tok1@github.com/a/b "
            "and https://tok2@github.com/c/d"
        )
        result = self.redact(line)
        self.assertNotIn("tok1", result)
        self.assertNotIn("tok2", result)
        self.assertEqual(result.count("https://github.com"), 2)

    def test_empty_string_unchanged(self):
        self.assertEqual(self.redact(""), "")


# ── _slugify ──────────────────────────────────────────────────────────────────

class TestSlugify(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.cloud_project import _slugify
        self.slugify = _slugify

    def test_simple_name(self):
        self.assertEqual(self.slugify("My Project"), "my-project")

    def test_uppercase_lowercased(self):
        self.assertEqual(self.slugify("UPPER"), "upper")

    def test_spaces_become_hyphens(self):
        self.assertEqual(self.slugify("hello world"), "hello-world")

    def test_special_chars_replaced(self):
        self.assertEqual(self.slugify("Hello, World!"), "hello-world")

    def test_consecutive_specials_one_hyphen(self):
        self.assertEqual(self.slugify("a  b"), "a-b")
        self.assertEqual(self.slugify("a--b"), "a-b")

    def test_leading_trailing_hyphens_stripped(self):
        self.assertEqual(self.slugify("-hello-"), "hello")

    def test_digits_preserved(self):
        self.assertEqual(self.slugify("Project 2025"), "project-2025")

    def test_empty_returns_project_fallback(self):
        self.assertEqual(self.slugify(""), "project")

    def test_none_returns_project_fallback(self):
        self.assertEqual(self.slugify(None), "project")

    def test_non_ascii_stripped(self):
        result = self.slugify("Ñoño")
        self.assertNotIn("Ñ", result)
        self.assertNotIn("ñ", result)


# ── ExecutorRegistry ──────────────────────────────────────────────────────────

class TestExecutorRegistry(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.registry import ExecutorRegistry
        self.reg = ExecutorRegistry()

    def test_register_and_get(self):
        class FakeExecutor:
            pass
        self.reg.register("test_job", FakeExecutor)
        self.assertIs(self.reg.get("test_job"), FakeExecutor)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.reg.get("nonexistent"))

    def test_duplicate_same_class_silent_noop(self):
        """Same class re-registering on a worker reload is a no-op.

        Registry must tolerate this — it happens any time the Python
        process re-imports the module (test harness, queue_job worker
        restart, dev autoreload).
        """
        class A:
            pass
        self.reg.register("dup_job", A)
        # Same class again → silent, no exception, binding unchanged.
        self.reg.register("dup_job", A)
        self.assertIs(self.reg.get("dup_job"), A)

    def test_duplicate_different_class_keeps_first_with_warning(self):
        """Two distinct classes registering for the same job_type
        happens when the saas-manager and the tenant module both ship
        a host_hardening executor and live in the same Python process
        (different DBs). The registry must NOT raise — it logs a
        warning and keeps whichever class registered first."""
        import logging

        class First:
            pass

        class Second:
            pass

        self.reg.register("collide_job", First)
        with self.assertLogs(
            'odoo.addons.incubacloud.models.registry',
            level=logging.WARNING,
        ) as cap:
            self.reg.register("collide_job", Second)
        # First wins.
        self.assertIs(self.reg.get("collide_job"), First)
        # Warning mentions both classes for traceability.
        joined = '\n'.join(cap.output)
        self.assertIn('First', joined)
        self.assertIn('Second', joined)

    def test_all_returns_copy_not_reference(self):
        class B:
            pass
        self.reg.register("b_job", B)
        snapshot = self.reg.all()
        self.assertIn("b_job", snapshot)
        # Mutating the returned dict must not affect the registry
        snapshot["b_job"] = None
        self.assertIs(self.reg.get("b_job"), B)

    def test_multiple_types_independent(self):
        class X:
            pass
        class Y:
            pass
        self.reg.register("x_job", X)
        self.reg.register("y_job", Y)
        self.assertIs(self.reg.get("x_job"), X)
        self.assertIs(self.reg.get("y_job"), Y)


# ── _normalize_domain ─────────────────────────────────────────────────────────

class TestNormalizeDomain(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.controllers.data_load import _normalize_domain
        self.normalize = _normalize_domain

    def test_strips_https(self):
        self.assertEqual(self.normalize("https://example.com"), "example.com")

    def test_strips_http(self):
        self.assertEqual(self.normalize("http://example.com"), "example.com")

    def test_strips_trailing_slash(self):
        self.assertEqual(self.normalize("example.com/"), "example.com")

    def test_strips_protocol_and_trailing_slash(self):
        self.assertEqual(self.normalize("https://example.com/"), "example.com")

    def test_plain_domain_unchanged(self):
        self.assertEqual(self.normalize("example.com"), "example.com")

    def test_none_returns_none(self):
        self.assertIsNone(self.normalize(None))

    def test_empty_returns_empty(self):
        self.assertEqual(self.normalize(""), "")

    def test_subdomain_preserved(self):
        self.assertEqual(
            self.normalize("https://app.example.com"),
            "app.example.com",
        )

    def test_path_stripped(self):
        self.assertEqual(
            self.normalize("https://example.com/some/path/"),
            "example.com/some/path",
        )


# ── _parse_github_repo_path ───────────────────────────────────────────────────

class TestParseGitHubRepoPath(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.controllers.data_load import _parse_github_repo_path
        self.parse = _parse_github_repo_path

    def test_https_url(self):
        owner, repo = self.parse("https://github.com/acme/myrepo")
        self.assertEqual(owner, "acme")
        self.assertEqual(repo, "myrepo")

    def test_https_url_with_git_suffix(self):
        owner, repo = self.parse("https://github.com/acme/myrepo.git")
        self.assertEqual(owner, "acme")
        self.assertEqual(repo, "myrepo")

    def test_ssh_url(self):
        owner, repo = self.parse("git@github.com:acme/myrepo")
        self.assertEqual(owner, "acme")
        self.assertEqual(repo, "myrepo")

    def test_ssh_url_with_git_suffix(self):
        owner, repo = self.parse("git@github.com:acme/myrepo.git")
        self.assertEqual(owner, "acme")
        self.assertEqual(repo, "myrepo")

    def test_trailing_slash_ignored(self):
        owner, repo = self.parse("https://github.com/acme/myrepo/")
        self.assertEqual(owner, "acme")
        self.assertEqual(repo, "myrepo")

    def test_invalid_url_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.parse("not-a-github-url")

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            self.parse("")

    def test_none_raises(self):
        with self.assertRaises(ValueError):
            self.parse(None)


# ── _smtp_canonical_domain ────────────────────────────────────────────────────

class TestSmtpCanonicalDomain(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            _smtp_canonical_domain,
        )
        self._fn = _smtp_canonical_domain

    def _inst(self, smtp_relay_user='', smtp_relay_host=''):
        from types import SimpleNamespace
        return SimpleNamespace(
            smtp_relay_user=smtp_relay_user,
            smtp_relay_host=smtp_relay_host,
        )

    def test_user_email_returns_domain_part(self):
        self.assertEqual(
            self._fn(self._inst(smtp_relay_user='user@example.com')),
            'example.com',
        )

    def test_user_email_subdomain_preserved(self):
        self.assertEqual(
            self._fn(self._inst(smtp_relay_user='user@mail.example.com')),
            'mail.example.com',
        )

    def test_user_without_at_falls_back_to_host(self):
        result = self._fn(self._inst(
            smtp_relay_user='noatsign',
            smtp_relay_host='mail.example.com',
        ))
        self.assertEqual(result, 'example.com')

    def test_host_three_labels_strips_first(self):
        self.assertEqual(
            self._fn(self._inst(smtp_relay_host='smtp.send.io')),
            'send.io',
        )

    def test_host_two_labels_returned_as_is(self):
        self.assertEqual(
            self._fn(self._inst(smtp_relay_host='example.com')),
            'example.com',
        )

    def test_empty_returns_empty_string(self):
        self.assertEqual(self._fn(self._inst()), '')

    def test_user_takes_priority_over_host(self):
        result = self._fn(self._inst(
            smtp_relay_user='user@primary.com',
            smtp_relay_host='mail.secondary.com',
        ))
        self.assertEqual(result, 'primary.com')


# ── _base_url logic ───────────────────────────────────────────────────────────

class TestBaseUrlLogic(BaseCase):
    """Test the _base_url() logic in isolation (mirrors DeployInstanceExecutor)."""

    @staticmethod
    def _base_url(domain):
        domain = (domain or '').strip()
        if not domain:
            return ''
        if domain.startswith(('http://', 'https://')):
            return domain.rstrip('/')
        return f'https://{domain}'

    def test_plain_domain_gets_https(self):
        self.assertEqual(self._base_url('example.com'), 'https://example.com')

    def test_https_domain_unchanged(self):
        self.assertEqual(self._base_url('https://example.com'), 'https://example.com')

    def test_http_domain_unchanged(self):
        self.assertEqual(self._base_url('http://example.com'), 'http://example.com')

    def test_trailing_slash_stripped(self):
        self.assertEqual(self._base_url('https://example.com/'), 'https://example.com')

    def test_empty_returns_empty(self):
        self.assertEqual(self._base_url(''), '')

    def test_none_returns_empty(self):
        self.assertEqual(self._base_url(None), '')

    def test_subdomain(self):
        self.assertEqual(self._base_url('app.example.com'), 'https://app.example.com')


# ── is_transient_connection_error ─────────────────────────────────────────────

class TestIsTransientConnectionError(BaseCase):
    """The classifier decides which executor exceptions are worth retrying.

    Transient connection failures (host briefly unreachable) must retry;
    permanent failures (bad credentials, unverifiable host key) and any
    non-connection error must fail fast.
    """

    def setUp(self):
        from odoo.addons.incubacloud.models.abstract_executor import (
            is_transient_connection_error,
        )
        self.f = is_transient_connection_error

    def test_asyncssh_connection_lost_is_transient(self):
        import asyncssh
        self.assertTrue(self.f(asyncssh.ConnectionLost('Connection lost')))

    def test_builtin_timeout_is_transient(self):
        # asyncssh surfaces a socket connect timeout as TimeoutError.
        self.assertTrue(self.f(TimeoutError(110, 'Connect call failed')))

    def test_connection_refused_is_transient(self):
        self.assertTrue(self.f(ConnectionRefusedError()))

    def test_dns_failure_is_transient(self):
        import socket
        self.assertTrue(self.f(socket.gaierror('name resolution failed')))

    def test_oserror_host_unreachable_is_transient(self):
        import errno
        self.assertTrue(self.f(OSError(errno.EHOSTUNREACH, 'no route to host')))

    def test_auth_failure_is_not_transient(self):
        # Wrong credentials never recover on retry — must fail fast.
        import asyncssh
        self.assertFalse(self.f(asyncssh.PermissionDenied('bad creds')))

    def test_host_key_failure_is_not_transient(self):
        import asyncssh
        self.assertFalse(self.f(asyncssh.HostKeyNotVerifiable('bad key')))

    def test_generic_error_is_not_transient(self):
        # A failed boot test / module update is a real failure, not a blip.
        self.assertFalse(self.f(RuntimeError('safe boot check failed')))

    def test_unrelated_oserror_is_not_transient(self):
        import errno
        self.assertFalse(self.f(OSError(errno.ENOENT, 'missing file')))
