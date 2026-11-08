"""The panel's git reader (plan-gitlab.md §4.2, 8-nov-2026).

The panel reads a GitLab repository with git itself, as a subprocess: a
HOME of its own, no system config, no prompt, https only, no redirects,
the credential in an ``extraHeader`` of a temporary ``.gitconfig`` and
never in argv. With the ``public`` policy the address checked by the
panel is pinned with ``http.curloptResolve``, which a git older than
2.37 accepts and ignores: such a git is refused before any network.

The settings travel in ``GIT_CONFIG_COUNT``/``KEY``/``VALUE``, never in a
file a SIGKILL would leave behind with the token; git's output goes to a
file of the session and is read only within its cap (security review of
G1, 8-nov-2026).
"""
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models._git_urls import RepoRef
from odoo.addons.incubacloud.net import git_reader

_REF = RepoRef(
    "gitlab", "https://gitlab.example", "gitlab.example", "acme/addons",
    "https://gitlab.example/Acme/addons.git",
)
_CRED = ("oauth2", "glpat-secret-token-123")
_SHA = "a" * 40


def _env_config(env):
    """Return the settings *env* hands git, one ``key = value`` per line.

    :param dict env: a git call's environment
    :rtype: str
    """
    return "\n".join(
        f"{env[f'GIT_CONFIG_KEY_{i}']} = {env[f'GIT_CONFIG_VALUE_{i}']}"
        for i in range(int(env.get("GIT_CONFIG_COUNT") or 0))
    )


def _feed(done, kwargs):
    """Write *done*'s output where the call asked for it, as git would.

    :param subprocess.CompletedProcess done: the answer
    :param dict kwargs: the call's keyword arguments
    :rtype: subprocess.CompletedProcess
    """
    to_files = False
    for name in ("stdout", "stderr"):
        target = kwargs.get(name)
        if hasattr(target, "write"):
            to_files = True
            target.write((getattr(done, name) or "").encode())
            target.flush()
    if to_files:
        return subprocess.CompletedProcess(done.args, done.returncode)
    return done


def _through_files(answer):
    """Wrap *answer* (``(argv, **kwargs) -> CompletedProcess``) with :func:`_feed`.

    :rtype: callable
    """
    def run(argv, **kwargs):
        return _feed(answer(argv, **kwargs), kwargs)
    return run


class _Calls:
    """Record each git call: argv, env, the settings it saw and its HOME."""

    def __init__(self, answers):
        """:param answers: callable ``(argv) -> CompletedProcess``"""
        self.calls = []
        self.answers = answers

    def __call__(self, argv, **kwargs):
        """Stand in for ``subprocess.run``.

        :rtype: subprocess.CompletedProcess
        """
        env = kwargs.get("env") or {}
        home = env.get("HOME")
        self.calls.append({
            "argv": list(argv), "env": dict(env), "config": _env_config(env),
            "kwargs": kwargs.copy(),
            "files": sorted(os.listdir(home)) if home and os.path.isdir(home) else [],
        })
        return _feed(self.answers(argv), kwargs)


def _ok(stdout="", stderr=""):
    """Return a successful run.

    :rtype: subprocess.CompletedProcess
    """
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)


def _fail(stderr, code=128):
    """Return a failed run.

    :rtype: subprocess.CompletedProcess
    """
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


def _version(text="git version 2.39.5"):
    """Answer ``git --version`` with *text*, the rest with a ref list.

    :rtype: callable
    """
    def answer(argv):
        if argv[-1] == "--version":
            return _ok(text + "\n")
        return _ok(f"{_SHA}\trefs/heads/main\n")
    return answer


class TestGitReaderCommand(BaseCase):

    def _run(self, answers, **reader_kwargs):
        """Run ``ls_remote`` with *answers* and return the calls made.

        :rtype: list
        """
        calls = _Calls(answers)
        reader = git_reader.GitReader(**reader_kwargs)
        with patch.object(git_reader.subprocess, "run", side_effect=calls):
            heads = reader.ls_remote(_REF, _CRED, ["refs/heads/*"])
        self.assertEqual(heads, {"refs/heads/main": _SHA})
        return calls.calls

    def test_the_token_never_goes_in_argv_and_redirects_are_off(self):
        calls = self._run(_version())
        net = calls[-1]
        argv = " ".join(net["argv"])
        self.assertNotIn(_CRED[1], argv)
        self.assertIn("https://gitlab.example/Acme/addons.git", net["argv"])
        for option in (
            "credential.helper=", "protocol.allow=never",
            "protocol.https.allow=always", "http.followRedirects=false",
        ):
            self.assertIn(option, net["argv"])
        self.assertEqual(net["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(net["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("GIT_ASKPASS", net["env"])
        self.assertIn("extraHeader = Authorization: Basic ", net["config"])
        self.assertNotIn(_CRED[1], net["config"])  # base64 of user:token
        self.assertNotIn("sslCAInfo", net["config"])
        self.assertNotIn("curloptResolve", net["config"])

    def test_no_setting_nor_token_is_written_to_disk(self):
        calls = self._run(_version(), pin_ip="203.0.113.7")
        for call in calls:
            self.assertNotIn(".gitconfig", call["files"])
        net = calls[-1]
        self.assertIn("extraHeader = Authorization: Basic ", net["config"])
        self.assertGreaterEqual(int(net["env"]["GIT_CONFIG_COUNT"]), 3)

    def test_the_home_is_private_and_removed(self):
        calls = self._run(_version())
        home = calls[-1]["env"]["HOME"]
        self.assertFalse(Path(home).exists())

    def test_a_ca_goes_in_a_file_scoped_to_the_server(self):
        calls = self._run(_version(), ca_pem="-----BEGIN CERTIFICATE-----\nx\n")
        self.assertIn("http.https://gitlab.example/.sslCAInfo = ", calls[-1]["config"])

    def test_a_pinned_address_is_resolved_by_git_only_from_2_37(self):
        calls = self._run(_version(), pin_ip="203.0.113.7")
        self.assertIn(
            "curloptResolve = gitlab.example:443:203.0.113.7", calls[-1]["config"],
        )
        calls = _Calls(_version("git version 2.34.1"))
        reader = git_reader.GitReader(pin_ip="203.0.113.7")
        with patch.object(git_reader.subprocess, "run", side_effect=calls), \
                self.assertRaises(git_reader.GitReaderError) as caught:
            reader.ls_remote(_REF, _CRED, ["refs/heads/*"])
        self.assertEqual(caught.exception.code, "git_too_old")
        self.assertEqual([c["argv"][-1] for c in calls.calls], ["--version"])

    def test_without_a_pin_the_version_is_not_asked(self):
        calls = self._run(_version())
        self.assertEqual(len(calls), 1)

    def test_a_public_repository_goes_without_a_header(self):
        calls = _Calls(_version())
        with patch.object(git_reader.subprocess, "run", side_effect=calls):
            git_reader.GitReader().ls_remote(_REF, None, ["refs/heads/*"])
        self.assertNotIn("extraHeader", calls.calls[-1]["config"])


class TestGitReaderErrors(BaseCase):

    def _code(self, answer):
        """Return the error code ``ls_remote`` raises with *answer*.

        :rtype: str
        """
        with patch.object(
            git_reader.subprocess, "run", side_effect=_through_files(answer),
        ), self.assertRaises(git_reader.GitReaderError) as caught:
            git_reader.GitReader().ls_remote(_REF, _CRED, [])
        self.assertNotIn("gitlab.example", str(caught.exception))
        self.assertNotIn(_CRED[1], str(caught.exception))
        return caught.exception.code

    def test_stderr_maps_to_a_code_without_the_url(self):
        url = "'https://gitlab.example/x.git/'"
        for stderr, code in (
            (f"fatal: Authentication failed for {url}", "auth"),
            ((f"fatal: unable to access {url}: The requested URL returned "
              "error: 403"), "auth"),
            ((f"fatal: unable to access {url}: SSL certificate problem: "
              "unable to get local issuer certificate"), "tls"),
            (f"fatal: repository {url} not found", "not_found"),
            ((f"fatal: unable to access {url}: The requested URL returned "
              "error: 301"), "redirect"),
            ((f"fatal: unable to access {url}: Could not resolve host: "
              "gitlab.example"), "unreachable"),
            ((f"fatal: unable to access {url}: Failed to connect to "
              "gitlab.example port 443"), "unreachable"),
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    self._code(lambda argv, s=stderr, **kw: _fail(s)), code,
                )

    def test_a_timeout_is_its_own_code(self):
        def answer(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        self.assertEqual(self._code(answer), "timeout")

    def test_a_file_too_large_is_its_own_code(self):
        self.assertEqual(
            self._code(lambda argv, **kw: _fail("", code=-25)), "too_large",
        )


class TestGitReaderDeadline(BaseCase):
    """A panel request's reads share one deadline (security review of G1,
    8-nov-2026)."""

    def test_no_call_waits_past_the_deadline(self):
        calls = _Calls(_version())
        reader = git_reader.GitReader(deadline=time.monotonic() + 5)
        with patch.object(git_reader.subprocess, "run", side_effect=calls):
            reader.ls_remote(_REF, _CRED, ["refs/heads/*"])
        self.assertLessEqual(calls.calls[-1]["kwargs"]["timeout"], 5)

    def test_a_spent_deadline_runs_no_git(self):
        calls = _Calls(_version())
        reader = git_reader.GitReader(deadline=time.monotonic() - 1)
        with patch.object(git_reader.subprocess, "run", side_effect=calls), \
                self.assertRaises(git_reader.GitReaderError) as caught:
            reader.ls_remote(_REF, _CRED, ["refs/heads/*"])
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(calls.calls, [])


class TestGitReaderTree(BaseCase):

    def test_tree_fetches_without_blobs_and_lists_entries(self):
        listing = "".join(
            f"100644 blob {_SHA}\tmod_{i}/__manifest__.py\0" for i in range(3)
        )

        calls = _Calls(lambda argv: _ok(listing) if "ls-tree" in argv else _ok())
        with patch.object(git_reader.subprocess, "run", side_effect=calls):
            paths = git_reader.GitReader().tree(_REF, _CRED, "main")
        self.assertEqual(paths, [f"mod_{i}/__manifest__.py" for i in range(3)])
        fetch = next(c for c in calls.calls if "fetch" in c["argv"])
        self.assertIn("--filter=blob:none", fetch["argv"])
        self.assertIn("--depth=1", fetch["argv"])
        self.assertEqual(fetch["argv"][-1], "main")
        self.assertNotIn(_CRED[1], " ".join(fetch["argv"]))

    def test_tree_refuses_a_tree_over_the_entry_limit(self):
        listing = f"100644 blob {_SHA}\tf\0" * (git_reader.MAX_TREE_ENTRIES + 1)

        def answer(argv, **kwargs):
            return _ok(listing) if "ls-tree" in argv else _ok()
        with patch.object(
            git_reader.subprocess, "run", side_effect=_through_files(answer),
        ), self.assertRaises(git_reader.GitReaderError) as caught:
            git_reader.GitReader().tree(_REF, None, "main")
        self.assertEqual(caught.exception.code, "too_large")

    def test_a_listing_over_its_byte_cap_is_refused_unread(self):
        listing = f"100644 blob {_SHA}\t{'x' * 400}\0" * (
            git_reader.MAX_LISTING_BYTES // 400 + 1
        )

        def answer(argv, **kwargs):
            return _ok(listing) if "ls-tree" in argv else _ok()
        calls = _Calls(answer)
        with patch.object(git_reader.subprocess, "run", side_effect=calls), \
                self.assertRaises(git_reader.GitReaderError) as caught:
            git_reader.GitReader().tree(_REF, None, "main")
        self.assertEqual(caught.exception.code, "too_large")
        for call in calls.calls:
            self.assertNotIn("capture_output", call["kwargs"])
            self.assertTrue(hasattr(call["kwargs"]["stdout"], "write"))

    def test_a_file_over_its_cap_is_refused_unread(self):
        def answer(argv, **kwargs):
            if "cat-file" in argv:
                return _ok("x" * (git_reader.MAX_FILE_BYTES + 1))
            return _ok()
        calls = _Calls(answer)
        with patch.object(git_reader.subprocess, "run", side_effect=calls), \
                self.assertRaises(git_reader.GitReaderError) as caught:
            git_reader.GitReader().read_files(_REF, None, "main", ["big.txt"])
        self.assertEqual(caught.exception.code, "too_large")
        cat = next(c for c in calls.calls if "cat-file" in c["argv"])
        self.assertTrue(hasattr(cat["kwargs"]["stdout"], "write"))

    def test_a_revision_that_looks_like_an_option_is_refused(self):
        with patch.object(git_reader.subprocess, "run") as run, \
                self.assertRaises(ValueError):
            git_reader.GitReader().tree(_REF, None, "--upload-pack=x")
        run.assert_not_called()

    def test_read_files_answers_none_for_a_missing_file(self):
        def answer(argv, **kwargs):
            if "cat-file" in argv:
                if argv[-1].endswith(":requirements.txt"):
                    return _ok("requests==2\n")
                return _fail("fatal: path 'x' does not exist in 'FETCH_HEAD'")
            return _ok()
        with patch.object(
            git_reader.subprocess, "run", side_effect=_through_files(answer),
        ):
            files = git_reader.GitReader().read_files(
                _REF, None, "main", ["requirements.txt", "missing.txt"],
            )
        self.assertEqual(
            files, {"requirements.txt": "requests==2\n", "missing.txt": None},
        )
