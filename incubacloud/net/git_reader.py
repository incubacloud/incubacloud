"""The panel's reader of Git repositories outside GitHub (plan-gitlab.md §4.2).

The panel reads a GitLab repository's branches, tree and a few files
with git itself, run as a subprocess, so the same reads serve a deploy
token, which GitLab's API does not accept. Every call is closed off:

- a HOME of its own (0700, removed afterwards), ``GIT_CONFIG_NOSYSTEM``,
  no prompt and no askpass, ``credential.helper`` emptied;
- https only (``protocol.allow=never``, ``protocol.https.allow=always``)
  and no redirects (``http.followRedirects=false``): without ``.git``
  GitLab answers 301, and a redirect is how a token would be carried
  elsewhere;
- the credential as an ``Authorization: Basic`` ``extraHeader`` scoped to
  the server, handed in ``GIT_CONFIG_COUNT``/``KEY``/``VALUE`` (git 2.31),
  never in argv nor in a file: a SIGKILL of the panel left a
  ``.gitconfig`` with the token in ``/tmp`` (security review of G1,
  8-nov-2026). An older git ignores those variables and reads without
  credential, CA or pin: it fails, it does not leak;
- a private CA as ``sslCAInfo`` scoped to the server, from a temporary
  file;
- git's output in files of the session, read only within a cap: a blob or
  a listing never reaches memory whole (security review, 8-nov-2026);
- with the ``public`` network policy, the address the panel checked,
  pinned with ``http.curloptResolve``. A git older than 2.37 accepts that
  option and ignores it without a word (spike of 8-nov-2026, §5), so
  such a git is refused (``git_too_old``) before any network.

A failure is a :class:`GitReaderError` with a code, never git's text,
which names the URL. The caller resolves the credential from the ORM and
hands plain values: nothing here touches the database.
"""
import base64
import os
import re
import shutil
import subprocess
import tempfile

#: ``ls-remote`` from a panel route: what the editor waits for (§9).
LS_REMOTE_TIMEOUT = 15

#: Each fetch of a tree or of files (§9): the import has 120 s in all.
FETCH_TIMEOUT = 30

#: Disk a fetch may write, enforced with ``ulimit -f`` and checked after
#: (§9): a tree without blobs of a large OCA repository takes a few MB.
MAX_FETCH_BYTES = 64 * 1024 * 1024

#: Entries of a tree listed at most, as ``import_inspector.MAX_TREE_ENTRIES``.
MAX_TREE_ENTRIES = 20000

#: A file read at most (a ``requirements.txt``, a manifest).
MAX_FILE_BYTES = 1024 * 1024

#: The listing of a tree read at most: :data:`MAX_TREE_ENTRIES` entries
#: of about 200 bytes each, with room for long paths.
MAX_LISTING_BYTES = 16 * 1024 * 1024

#: Any other output of git read at most (``ls-remote`` of a few patterns).
MAX_OUTPUT_BYTES = 4 * 1024 * 1024

#: git's stderr read at most, to find its error code.
_MAX_STDERR_BYTES = 64 * 1024

#: The git that honours ``http.curloptResolve`` (2.37).
_MIN_PIN_VERSION = (2, 37)

#: What a revision may be: a branch name or a SHA, never an option.
_REV_RE = re.compile(r"(?!-)[A-Za-z0-9_/.\-]{1,200}")

#: A path inside the repository: relative, no ``..``, no option.
_PATH_RE = re.compile(r"(?!-)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_./ +@=-]{1,500}")

#: git's stderr, read in this order, to an error code.
_STDERR_CODES = (
    (re.compile(r"returned error: 30[0-9]|redirect", re.IGNORECASE), "redirect"),
    (re.compile(
        r"Authentication failed|returned error: 40[13]|could not read "
        r"Username|HTTP Basic: Access denied", re.IGNORECASE,
    ), "auth"),
    (re.compile(r"SSL|certificate|TLS", re.IGNORECASE), "tls"),
    (re.compile(
        r"not found|returned error: 404|does not appear to be a git "
        r"repository|couldn't find remote ref|not our ref|does not exist|"
        r"Not a valid object name", re.IGNORECASE,
    ), "not_found"),
    (re.compile(r"File size limit|too large", re.IGNORECASE), "too_large"),
)

#: The signal ``ulimit -f`` kills a writer with.
_SIGXFSZ = 25


class GitReaderError(Exception):
    """A read that failed, with its code and no URL.

    ``code`` is one of ``auth``, ``tls``, ``not_found``, ``unreachable``,
    ``private_address``, ``timeout``, ``too_large``, ``redirect`` and
    ``git_too_old``.
    """

    def __init__(self, code):
        """:param str code: what failed"""
        super().__init__(code)
        self.code = code


def _code_of(stderr):
    """Return the error code git's *stderr* means.

    :param str stderr: what git wrote
    :rtype: str
    """
    for pattern, code in _STDERR_CODES:
        if pattern.search(stderr or ""):
            return code
    return "unreachable"


def _host_port(authority):
    """Return ``(host, port)`` of *authority*.

    :param str authority: ``host`` or ``host:port``
    :rtype: tuple
    """
    host, sep, port = authority.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return authority, 443


class GitReader:
    """Read a repository of one Git server with the panel's git.

    :param str ca_pem: the server's private CA bundle, or None
    :param str pin_ip: the address to dial, checked by the caller under
        the ``public`` policy; None with ``any``
    :param str git: the git binary
    """

    def __init__(self, ca_pem=None, pin_ip=None, git="git"):
        self.ca_pem = ca_pem
        self.pin_ip = pin_ip
        self.git = git

    # ── Public reads ───────────────────────────────────────────────────

    def ls_remote(self, ref, credential, patterns=()):
        """Return the refs of *ref* matching *patterns*.

        :param RepoRef ref: the repository
        :param credential: ``(username, token)``, or None for a public one
        :param patterns: ``ls-remote`` patterns, such as ``refs/heads/*``
        :return: ``{refname: sha}``
        :rtype: dict
        :raises GitReaderError: when git fails
        """
        with self._session(ref, credential) as run:
            out = run(
                ["ls-remote", ref.canonical, *patterns],
                LS_REMOTE_TIMEOUT,
            )
        heads = {}
        for line in out.splitlines():
            sha, _sep, name = line.partition("\t")
            if name:
                heads[name] = sha
        return heads

    def tree(self, ref, credential, rev, path=""):
        """Return the file paths of *ref* at *rev*, under *path* if given.

        A fetch of depth 1 without blobs, then ``ls-tree -r``.

        :param RepoRef ref: the repository
        :param credential: ``(username, token)`` or None
        :param str rev: a branch name or a SHA
        :param str path: a folder of the repository, or '' for all
        :rtype: list
        :raises ValueError: on a revision or path that could be an option
        :raises GitReaderError: when git fails or the tree is too large
        """
        self._check_rev(rev)
        if path:
            self._check_path(path)
        with self._session(ref, credential) as run:
            self._fetch(run, ref, rev)
            argv = ["-C", "repo", "ls-tree", "-r", "-z", "--full-tree", "FETCH_HEAD"]
            if path:
                argv += ["--", path]
            out = run(argv, FETCH_TIMEOUT, MAX_LISTING_BYTES)
        entries = [e for e in out.split("\0") if e]
        if len(entries) > MAX_TREE_ENTRIES:
            raise GitReaderError("too_large")
        return [entry.split("\t", 1)[1] for entry in entries if "\t" in entry]

    def read_files(self, ref, credential, rev, paths):
        """Return the text of each of *paths* in *ref* at *rev*.

        :param RepoRef ref: the repository
        :param credential: ``(username, token)`` or None
        :param str rev: a branch name or a SHA
        :param paths: paths inside the repository
        :return: ``{path: text or None}``, None for a missing file
        :rtype: dict
        :raises ValueError: on a revision or path that could be an option
        :raises GitReaderError: when git fails or a file is too large
        """
        self._check_rev(rev)
        for path in paths:
            self._check_path(path)
        files = {}
        with self._session(ref, credential) as run:
            self._fetch(run, ref, rev)
            for path in paths:
                try:
                    out = run(
                        ["-C", "repo", "cat-file", "-p", f"FETCH_HEAD:{path}"],
                        FETCH_TIMEOUT, MAX_FILE_BYTES,
                    )
                except GitReaderError as exc:
                    if exc.code == "not_found":
                        files[path] = None
                        continue
                    raise
                files[path] = out
        return files

    # ── Plumbing ───────────────────────────────────────────────────────

    @staticmethod
    def _check_rev(rev):
        """Refuse a revision that is not a plain branch name or SHA.

        :raises ValueError: when *rev* could be read as an option
        """
        if not isinstance(rev, str) or not _REV_RE.fullmatch(rev) or ".." in rev:
            raise ValueError("invalid revision")

    @staticmethod
    def _check_path(path):
        """Refuse a repository path that could escape or be an option.

        :raises ValueError: when *path* is not a plain relative path
        """
        if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
            raise ValueError("invalid path")

    def _fetch(self, run, ref, rev):
        """Fetch *rev* of *ref* at depth 1, without blobs, into ``repo``.

        The remote is named: without a named remote git keeps no
        promisor and cannot fetch a blob on demand (spike, 8-nov-2026).
        """
        run(["init", "-q", "--bare", "repo"], FETCH_TIMEOUT)
        run(["-C", "repo", "remote", "add", "origin", ref.canonical], FETCH_TIMEOUT)
        run(
            ["-C", "repo", "fetch", "-q", "--depth=1", "--no-tags",
             "--filter=blob:none", "origin", rev],
            FETCH_TIMEOUT,
        )
        if _tree_bytes(run.__self__.home) > MAX_FETCH_BYTES:
            raise GitReaderError("too_large")

    def _session(self, ref, credential):
        """Return a context manager giving a ``run(argv, timeout)``.

        :rtype: _Session
        """
        return _Session(self, ref, credential)

    def _version_ok(self, env):
        """Return whether the panel's git honours ``http.curloptResolve``.

        :rtype: bool
        """
        try:
            done = subprocess.run(
                [self.git, "--version"], capture_output=True, text=True,
                timeout=5, env=env, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        found = re.search(r"(\d+)\.(\d+)", done.stdout or "")
        return bool(found) and (
            (int(found.group(1)), int(found.group(2))) >= _MIN_PIN_VERSION
        )


class _Session:
    """One call's private HOME, git settings and runner."""

    def __init__(self, reader, ref, credential):
        """Keep what the session builds its settings from."""
        self.reader = reader
        self.ref = ref
        self.credential = credential
        self.home = None
        self.config = []

    def __enter__(self):
        """Make the HOME and the settings; return the runner.

        :rtype: callable
        :raises GitReaderError: ``git_too_old`` when an address is to be
            pinned and the panel's git would ignore it
        """
        self.home = tempfile.mkdtemp(prefix="ic-git-")
        os.chmod(self.home, 0o700)
        env = self._env()
        if self.reader.pin_ip and not self.reader._version_ok(env):
            self.__exit__(None, None, None)
            raise GitReaderError("git_too_old")
        self.config = self._settings()
        return self._run

    def __exit__(self, *exc):
        """Remove the HOME and everything fetched into it."""
        if self.home:
            shutil.rmtree(self.home, ignore_errors=True)
            self.home = None
        return False

    def _env(self):
        """Return the environment of every git call: no system, no prompt.

        With the session's settings in ``GIT_CONFIG_COUNT``, ``KEY_<n>``
        and ``VALUE_<n>``, once :meth:`_settings` has made them.

        :rtype: dict
        """
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_") and key not in ("SSH_ASKPASS",)
        }
        env.update({
            "HOME": self.home,
            "XDG_CONFIG_HOME": self.home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        if self.config:
            env["GIT_CONFIG_COUNT"] = str(len(self.config))
            for i, (key, value) in enumerate(self.config):
                env[f"GIT_CONFIG_KEY_{i}"] = key
                env[f"GIT_CONFIG_VALUE_{i}"] = value
        return env

    def _settings(self):
        """Return the credential, CA and pin settings, as ``(key, value)`` pairs.

        The CA goes in a file of the HOME (0600): it is not a secret.

        :rtype: list
        """
        scope = f"http.https://{self.ref.authority}/"
        pairs = [(f"{scope}.followRedirects", "false")]
        if self.credential:
            pair = f"{self.credential[0]}:{self.credential[1]}".encode()
            pairs.append((
                f"{scope}.extraHeader",
                "Authorization: Basic " + base64.b64encode(pair).decode(),
            ))
        if self.reader.ca_pem:
            ca_path = os.path.join(self.home, "ca.pem")
            _write_private(ca_path, self.reader.ca_pem)
            pairs.append((f"{scope}.sslCAInfo", ca_path))
        if self.reader.pin_ip:
            host, port = _host_port(self.ref.authority)
            pairs.append((
                "http.curloptResolve", f"{host}:{port}:{self.reader.pin_ip}",
            ))
        return pairs

    def _run(self, argv, timeout, limit=MAX_OUTPUT_BYTES):
        """Run git with *argv* in the session; return its stdout.

        stdout and stderr go to files of the HOME, under ``ulimit -f``;
        stdout is read only when it is within *limit* bytes.

        :param list argv: git's arguments
        :param int timeout: seconds
        :param int limit: the most bytes of stdout read
        :rtype: str
        :raises GitReaderError: on any failure, with its code; ``too_large``
            when the output passes *limit*
        """
        blocks = MAX_FETCH_BYTES // 512
        full = [
            "/bin/sh", "-c", f'ulimit -f {blocks} && exec "$@"', "sh",
            self.reader.git,
            "-c", "credential.helper=",
            "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always",
            "-c", "http.followRedirects=false",
            *argv,
        ]
        out_path = os.path.join(self.home, ".ic-out")
        err_path = os.path.join(self.home, ".ic-err")
        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                done = subprocess.run(
                    full, stdout=out, stderr=err, timeout=timeout,
                    env=self._env(), cwd=self.home, check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise GitReaderError("timeout") from exc
        except OSError as exc:
            raise GitReaderError("unreachable") from exc
        if done.returncode in (-_SIGXFSZ, 128 + _SIGXFSZ):
            raise GitReaderError("too_large")
        if done.returncode != 0:
            raise GitReaderError(_code_of(_read_head(err_path, _MAX_STDERR_BYTES)))
        if os.path.getsize(out_path) > limit:
            raise GitReaderError("too_large")
        return _read_head(out_path, limit)


def _write_private(path, text):
    """Write *text* to *path*, readable by its owner only.

    :param str path: a file in the session's HOME
    :param str text: its content
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read_head(path, limit):
    """Return at most *limit* bytes of *path*, decoded as UTF-8.

    :param str path: a file of the session
    :param int limit: the most bytes read
    :rtype: str
    """
    with open(path, "rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace")


def _tree_bytes(root):
    """Return the bytes the files under *root* take.

    :param str root: a directory
    :rtype: int
    """
    total = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue
    return total
