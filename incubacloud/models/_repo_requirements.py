"""Utilities for repo addon/requirement management.

Includes:
- Repo alias and addon list helpers (used by deploy/rebuild executors)
- Pre-flight addon conflict detection across repos
- Pip requirement merging with git-style conflict markers
- GitHub API helpers for fetching module lists and requirements.txt

Conflicts are stored as git-style markers in pip_dependencies so the user
can see exactly which repo introduced each version:

    <<<<<<< existing
    requests==2.28.0
    =======
    requests>=2.30.0
    >>>>>>> org/repo (main)

The field is the single source of truth.  Conflict detection at deploy
time reads the field directly — no alert lookup needed.
"""

import asyncio
import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import namedtuple
from contextlib import suppress

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion

from odoo.exceptions import UserError

from ..github.http_utils import read_json_limited, safe_urlopen

from ..github.client import GitHubAppClient, GitHubAPIError, GitHubPATClient
from ..net.git_reader import GitReaderError
from ._git_urls import RepoRef, parse_provider_repo_url
from .cloud_rate_limit import consume_quota

#: What a read of a GitLab repository needs, resolved from the ORM on
#: the calling thread so the read itself may run in a worker thread:
#: the provider's reader (CA, pinned address), the repository and its
#: ``(username, token)`` or None for a public one.
GitSource = namedtuple('GitSource', 'reader ref credential')

_logger = logging.getLogger(__name__)


# ── Repo helpers ───────────────────────────────────────────────────────────

def _repo_alias(url):
    """Derive a short alias from a repo URL: last path component without .git."""
    url = (url or "").strip().rstrip("/").removesuffix(".git")
    return url.split("/")[-1] or ""


#: What doodba keeps in ``custom/src`` for itself, which a repository
#: fetched into ``custom/src/<alias>`` would replace: its private addons
#: and its repos and addons files, read as .yaml or .yml (doodbalib).
#: ``deploy.sh``'s ``ic_reserved_alias`` refuses the same (security
#: review of G1, 8-nov-2026).
RESERVED_REPO_ALIASES = frozenset({
    'private', 'repos.yaml', 'repos.yml', 'addons.yaml', 'addons.yml',
})


def _parse_repo_addons(addons_str):
    """Parse addons spec into a list; empty string means all (*)."""
    if not (addons_str or "").strip():
        return ["*"]
    items = [m.strip() for m in re.split(r"[,\n]", addons_str) if m.strip()]
    return items or ["*"]


def _apply_excludes(base_list, excludes_str):
    """Remove excluded names from *base_list*.

    *base_list* must be a resolved list (no ``"*"``).
    Returns the filtered list, or ``["*"]`` if nothing remains.
    """
    excl = {
        m.strip()
        for m in re.split(r"[,\n]", excludes_str or "")
        if m.strip()
    }
    result = [m for m in base_list if m not in excl]
    return result or ["*"]


# ── Conflict marker pattern ────────────────────────────────────────────────

CONFLICT_RE = re.compile(
    r'<<<<<<< (?P<existing_src>[^\n]*)\n'
    r'(?P<existing_line>[^\n]*)\n'
    r'=======\n'
    r'(?P<incoming_line>[^\n]*)\n'
    r'>>>>>>> (?P<incoming_src>[^\n]*)',
)


# ── GitHub URL parser ──────────────────────────────────────────────────────

# Anchored at both ends to prevent attacker-prefixed URLs (e.g.
# ``https://attacker.tld/foo/github.com/u/r``) from matching. Mirrors
# the regex in ``controllers/_data_load/_helpers.py``.
_GH_URL_RE = re.compile(
    r'^(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)'
    r'([^/]+)/([^/]+)$'
)

# The one form a repository line keeps, and the only one the GitHub
# token travels with (5-nov-2026): https, the host exactly
# ``github.com`` (no ``www.``, user, password or port), an owner as
# GitHub names them and a repository name, ``.git`` or not. A host
# found anywhere else in the URL, as in
# ``https://evil.example/github.com/x.git``, matches nothing.
_CANONICAL_REPO_URL_RE = re.compile(
    r'https://github\.com/[A-Za-z0-9][A-Za-z0-9_-]{0,38}/'
    r'(?!\.{0,2}(?:\.git)?$)[A-Za-z0-9._-]{1,100}'
)

# The spellings of a repository on github.com that
# :func:`canonical_repo_url` rewrites into the canonical form. ASCII
# case only: Unicode folding would take ``gıthub.com`` and
# ``GİTHUB.com`` for github.com (5-nov-2026).
_GH_SPELLING_RE = re.compile(
    r'(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)'
    r'([^/]+)/([^/]+?)/?',
    re.IGNORECASE | re.ASCII,
)


# ── Git ref / SHA validation ───────────────────────────────────────────────
#
# These regex are the sole defense against shell injection through values
# that flow into ``git fetch``, ``gitaggregate``, or ``repos.yaml`` during
# deploy/rebuild. Branches and commit SHAs come from two untrusted
# sources:
#
#   1. GitHub webhooks (push.ref, pull_request.head.ref) — an attacker
#      with permission to push to the repo can choose ref names like
#      ``main --upload-pack=evil`` or ``main; rm -rf /``.
#
#   2. Direct frontend writes / ORM RPC by users with write access to
#      ``cloud.instance.repo`` / ``cloud.project.repo``.
#
# A leading ``-`` is rejected so a value cannot be misinterpreted as a
# command-line flag by any tool that consumes it. ``..`` is rejected to
# avoid path-traversal-like surprises in tooling.
_GIT_REF_RE = re.compile(r'^(?!-)[A-Za-z0-9_/.\-]{1,200}$')
_GIT_SHA_RE = re.compile(r'^[a-f0-9]{7,64}$')


def is_safe_git_ref(value):
    """Return True if ``value`` is a syntactically safe git ref name."""
    if not isinstance(value, str):
        return False
    if '..' in value:
        return False
    return bool(_GIT_REF_RE.fullmatch(value))


def is_safe_git_sha(value):
    """Return True if ``value`` is a syntactically safe git commit SHA."""
    return isinstance(value, str) and bool(_GIT_SHA_RE.fullmatch(value))


def is_safe_repo_url(value):
    """Return True if ``value`` is a GitHub repository URL in the canonical form.

    Only ``https://github.com/<owner>/<repo>`` and its ``.git`` form,
    exactly (``_CANONICAL_REPO_URL_RE``): the build clones it with the
    panel's GitHub token, so a URL that could name another host, a
    user, a port or plain http is refused (5-nov-2026). Other spellings
    of a github.com repository are rewritten by
    :func:`canonical_repo_url` first. A repository of a configured Git
    provider (GitLab) is checked by :func:`resolve_repo_ref` instead,
    against the providers' base URLs (plan-gitlab.md §4.1, 8-nov-2026).
    """
    return isinstance(value, str) and bool(
        _CANONICAL_REPO_URL_RE.fullmatch(value)
    )


def canonical_repo_url(value):
    """Return *value* in the canonical form when it names a github.com repo.

    ``git@github.com:``, ``http://``, no scheme, ``www.``, any case of
    the host, a final slash and surrounding blanks all become
    ``https://github.com/<owner>/<repo>``, keeping ``.git`` when it
    was there. Anything else comes back stripped and otherwise as it
    was, for :func:`is_safe_repo_url` to refuse.

    :param str value: a repository URL as a user, an import or a
        caller spelled it
    :return: the canonical URL, or *value* stripped
    :rtype: str
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = _GH_SPELLING_RE.fullmatch(text)
    if not match:
        return text
    candidate = f'https://github.com/{match.group(1)}/{match.group(2)}'
    return candidate if is_safe_repo_url(candidate) else text


def _provider_bases(env):
    """Return the canonical base URLs of the active Git providers.

    :param env: an Odoo environment
    :rtype: list
    """
    return env['cloud.git.provider'].sudo().search([]).mapped('base_url')


def resolve_repo_ref(env, url):
    """Return the :data:`RepoRef` a repository URL names, or None.

    A github.com repository in any spelling :func:`canonical_repo_url`
    rewrites gives ``kind='github'``, its ``owner/repo`` in lower case
    as path; a repository of a configured provider gives
    ``kind='gitlab'`` (``_git_urls.parse_provider_repo_url``). Anything
    else, None.

    :param env: an Odoo environment
    :param str url: the URL as typed or stored
    :rtype: RepoRef or None
    """
    if not isinstance(url, str):
        return None
    github = canonical_repo_url(url)
    if is_safe_repo_url(github):
        owner, repo = _parse_github_repo_path(github)
        return RepoRef(
            'github', 'https://github.com', 'github.com',
            f'{owner}/{repo}'.lower(), github,
        )
    return parse_provider_repo_url(url, _provider_bases(env))


def provider_of(env, ref):
    """Return the ``cloud.git.provider`` of *ref* (sudo), empty for GitHub.

    :param env: an Odoo environment
    :param RepoRef ref: a reference :func:`resolve_repo_ref` returned
    :rtype: cloud.git.provider
    """
    Provider = env['cloud.git.provider'].sudo()
    if not ref or ref.kind != 'gitlab':
        return Provider.browse()
    return Provider.search([('base_url', '=', ref.base)], limit=1)


def canonical_any_repo_url(env, url):
    """Return *url* canonical, of GitHub or of a configured provider.

    :param env: an Odoo environment
    :param str url: the URL as typed or stored
    :return: the canonical URL, or *url* stripped when it names no
        known repository
    :rtype: str
    """
    if not isinstance(url, str):
        return url
    ref = resolve_repo_ref(env, url)
    return ref.canonical if ref else url.strip()


def consume_hourly_quota(env, bucket, cap_key):
    """Count one hit on *bucket* against an hourly cap, in its own transaction.

    :func:`~.cloud_rate_limit.consume_quota` with a window of an hour:
    committed at once, at READ COMMITTED, so parallel hits wait for the
    bucket's row and never pass the cap.

    :param env: an Odoo environment, of the user who asks (its user and
        context are kept)
    :param str bucket: the per-user rate-limit bucket
    :param str cap_key: the ``cloud.settings`` field holding the cap
    :return: whether the hit is within the cap
    :rtype: bool
    """
    return consume_quota(env, bucket, cap_key=cap_key, window_seconds=3600)


def consume_git_read(env):
    """Count one git read against the user's hourly cap.

    ``rate_limit_git_reads_per_hour``, in the one bucket every read of a
    repository with git uses, the editor's, the panel's and the
    assistant's (:func:`consume_hourly_quota`): each one runs git or asks
    GitLab (plan-gitlab.md §9).

    :param env: an Odoo environment, of the user who reads
    :return: whether the read is within the cap
    :rtype: bool
    """
    return consume_hourly_quota(
        env, f'git_reads_user:{env.uid}', 'rate_limit_git_reads_per_hour',
    )


#: The GitLab repositories one panel request reads with git at most,
#: besides the user's hourly cap: each read is a git process and a
#: temporary fetch of up to 64 MB, and a request lists them all at once
#: (security review of G1, 8-nov-2026).
REQUEST_GIT_READS = 10

#: Seconds all of one panel request's GitLab reads take at most: what
#: the module check gives GitHub (``cloud_instance.ADDON_CHECK_SECONDS``).
REQUEST_GIT_SECONDS = 20


class GitReadBudget:
    """The GitLab reads one panel request may still make.

    At most :data:`REQUEST_GIT_READS`, all before one deadline
    (:data:`REQUEST_GIT_SECONDS` from now), and each one also one read of
    the user's hourly cap (:func:`consume_git_read`). Taken on the
    calling thread, which holds the ORM. A read past it is not made, and
    the request says so without failing: the deploy reads what it needs
    without any (security review of G1, 8-nov-2026).

    :param env: an Odoo environment, of the user who reads
    :param int max_reads: reads at most; None: :data:`REQUEST_GIT_READS`
    :param float seconds: seconds for them all; None:
        :data:`REQUEST_GIT_SECONDS`
    """

    def __init__(self, env, max_reads=None, seconds=None):
        """Start the request's count and its deadline now."""
        self.env = env
        self.left = REQUEST_GIT_READS if max_reads is None else max_reads
        self.deadline = time.monotonic() + (
            REQUEST_GIT_SECONDS if seconds is None else seconds
        )
        self.skipped = 0

    def take(self):
        """Count one read, and return whether it may be made.

        The hourly cap is asked last, so a read the request had no room
        for does not count against it.

        :rtype: bool
        """
        if (
            self.left <= 0
            or time.monotonic() >= self.deadline
            or not consume_git_read(self.env)
        ):
            self.skipped += 1
            return False
        self.left -= 1
        return True

    def requirements_warning(self):
        """Return what the request says of the requirements it did not read.

        :return: the text, or None when it read them all
        :rtype: str or None
        """
        if not self.skipped:
            return None
        return self.env._(
            "Limit of Git reads reached: the requirements.txt of %(count)s "
            "GitLab repositories was not read now. A deploy reads it from "
            "each branch that is not pinned.",
            count=self.skipped,
        )

    def addons_warning(self):
        """Return what the request says of the module lists it did not read.

        :return: the text, or None when it read them all
        :rtype: str or None
        """
        if not self.skipped:
            return None
        return self.env._(
            "Limit of Git reads reached: %(count)s GitLab repositories were "
            "not checked now for modules another repository also defines. "
            "The deploy checks them.",
            count=self.skipped,
        )


def on_git_provider(env, url):
    """Return whether *url* names a repository of a Git provider (GitLab).

    :param env: an Odoo environment
    :param str url: a repository URL
    :rtype: bool
    """
    ref = resolve_repo_ref(env, url)
    return bool(ref) and ref.kind == 'gitlab'


def user_may_use_credential(env, provider, path):
    """Return whether the user of *env* may read *path* with the panel's credential.

    A credential covers a whole namespace, more than any one user works
    on. It is used for a repository of a line the user sees under their
    record rules (a project's or an instance's), or, to add a new one,
    for a developer of a project they can edit; any other read goes as
    a public one (security review of G1, 8-nov-2026).

    :param env: an Odoo environment, of the user who reads
    :param provider: the ``cloud.git.provider``
    :param str path: the repository's path, lower case
    :rtype: bool
    """
    if env.su:
        return True
    domain = [('provider_id', '=', provider.id), ('repo_path', '=', path)]
    for model in ('cloud.project.repo', 'cloud.instance.repo'):
        if env[model].search_count(domain, limit=1):
            return True
    if not env['cloud.security.mixin']._has_cloud_group('group_cloud_developer'):
        return False
    return bool(env['cloud.project'].search([])._filtered_access('write'))


def resolve_git_source(env, url, for_user=False):
    """Return the :data:`GitSource` of a GitLab repository *url*, or None.

    None for a GitHub repository or an unknown host. Runs on the calling
    thread: it reads the provider and its credential from the ORM.

    :param env: an Odoo environment
    :param str url: the repository URL
    :param bool for_user: a read a user asked for: the credential only
        when :func:`user_may_use_credential`, else none (a public read)
    :rtype: GitSource or None
    :raises GitReaderError: ``private_address`` when the provider no
        longer resolves to public addresses under the ``public`` policy
    """
    ref = resolve_repo_ref(env, url)
    provider = provider_of(env, ref)
    if not provider:
        return None
    credential = None
    if not for_user or user_may_use_credential(env, provider, ref.path):
        credential = provider._git_credential(ref.path)
    return GitSource(provider._git_reader(), ref, credential)


def _addons_in(paths):
    """Return the addon names of a tree: folders holding ``__manifest__.py``.

    :param paths: file paths of a repository
    :rtype: list
    """
    return sorted({
        path.rsplit('/', 2)[-2]
        for path in paths
        if path.endswith('/__manifest__.py')
    })


def git_source_addons(source, branch):
    """Return the addons of a GitLab repository's *branch*.

    :param GitSource source: what :func:`resolve_git_source` returned
    :param str branch: the branch
    :rtype: list
    :raises GitReaderError: when the server cannot be read
    :raises ValueError: on a branch that could be an option
    """
    return _addons_in(source.reader.tree(source.ref, source.credential, branch))


def git_source_requirements(source, branch):
    """Return the ``requirements.txt`` of a GitLab repository's *branch*.

    :param GitSource source: what :func:`resolve_git_source` returned
    :param str branch: the branch
    :return: its text, or None when the branch has none
    :rtype: str or None
    :raises GitReaderError: when the server cannot be read
    :raises ValueError: on a branch that could be an option
    """
    return source.reader.read_files(
        source.ref, source.credential, branch, ['requirements.txt'],
    ).get('requirements.txt')


def _provider_read(env, url, read, branch, deadline=None):
    """Read a repository of a Git provider with *read*, if *url* names one.

    :param env: an Odoo environment
    :param str url: the repository URL
    :param read: :func:`git_source_addons` or :func:`git_source_requirements`
    :param str branch: the branch
    :param float deadline: the request's (:class:`GitReadBudget`), or None
    :return: ``(False, None)`` when *url* is not a provider's repository,
        else ``(True, what *read* returned or None when it failed)``
    :rtype: tuple
    """
    if not on_git_provider(env, url):
        return False, None
    with suppress(GitReaderError, ValueError):
        source = resolve_git_source(env, url)
        if source:
            source.reader.deadline = deadline
            return True, read(source, branch)
    return True, None


def _git_sources(env, repos):
    """Return ``{url: GitSource}`` of the GitLab lines among *repos*.

    :param env: an Odoo environment
    :param repos: repository lines
    :rtype: dict
    """
    sources = {}
    for repo in repos:
        with suppress(GitReaderError):
            source = resolve_git_source(env, repo.url)
            if source:
                sources[repo.url] = source
    return sources


def _source_addons_or_none(source, branch):
    """Return :func:`git_source_addons`, or None when the read fails.

    :param GitSource source: what :func:`resolve_git_source` returned
    :param str branch: the branch
    :rtype: list or None
    """
    with suppress(GitReaderError, ValueError):
        return git_source_addons(source, branch)
    return None


def _parse_github_repo_path(url):
    url = (url or '').strip().rstrip('/').removesuffix('.git')
    m = _GH_URL_RE.fullmatch(url)
    if not m:
        raise ValueError(f"Cannot parse GitHub URL: {url!r}")
    return m.group(1), m.group(2)


# ── Pip requirement line parser ────────────────────────────────────────────

def _parse_req_line(line):
    """Return (lowercase_name, raw_spec) or None."""
    stripped = line.split('#')[0].strip()
    if not stripped:
        return None
    m = re.match(r'^([A-Za-z0-9_.-]+)', stripped)
    return (m.group(1).lower(), stripped) if m else None


# ── Compatible specs ───────────────────────────────────────────────────────

def _pinned_version(specifier):
    """Return the version an ``==`` or ``===`` clause of *specifier* pins.

    :param specifier: a ``packaging`` ``SpecifierSet``
    :return: the pinned version string, or None when no clause pins one
        (a wildcard ``==2.*`` is a range, not a pin)
    """
    for clause in specifier:
        if clause.operator in ('==', '===') and not clause.version.endswith('.*'):
            return clause.version
    return None


def _line_satisfies(chosen, other):
    """Whether installing *chosen* also meets what *other* asks for.

    Decided only where it is certain: *other* names no version, or
    *chosen* pins one that *other*'s range admits, or every clause of
    *other* is already in *chosen*. Anything else (two ranges that
    overlap only in part, a URL on either side, different environment
    markers or extras) is left for a person, as before: a URL is no
    version, so nothing says a bare name is met by it or meets it.

    :param chosen: parsed ``Requirement`` of the line that would stay
    :param other: parsed ``Requirement`` of the line it would replace
    :rtype: bool
    """
    if chosen.url or other.url:
        return False
    if str(chosen.marker) != str(other.marker):
        return False
    if not other.extras <= chosen.extras:
        return False
    if not other.specifier:
        return True
    pin = _pinned_version(chosen.specifier)
    if pin is not None:
        try:
            return other.specifier.contains(pin, prereleases=True)
        except InvalidVersion:
            return False
    return set(other.specifier) <= set(chosen.specifier)


def pip_spec_relation(existing, incoming):
    """Tell whether two requirement lines of one package can both hold.

    ``requests`` and ``requests==2.34.2`` do not disagree: the pin
    meets the bare name, so a repo asking for any ``requests`` is
    served by the line another repo pinned (core 18.0 and OCA/queue,
    9-nov-2026). Two different pins, or a pin outside the other's
    range, do disagree and stay a conflict a person resolves.

    :param str existing: the line already in ``pip_dependencies``
    :param str incoming: the line a repo's ``requirements.txt`` brings
    :return: ``'existing'`` when the stored line already meets the
        incoming one, ``'incoming'`` when the incoming line is the
        stricter one and meets the stored one, ``None`` on a conflict
    :rtype: str or None
    """
    if existing.strip() == incoming.strip():
        return 'existing'
    try:
        old, new = Requirement(existing), Requirement(incoming)
    except InvalidRequirement:
        return None
    if _line_satisfies(old, new):
        return 'existing'
    if _line_satisfies(new, old):
        return 'incoming'
    return None


def _names_no_version(line):
    """Whether *line* names its package without a version or a URL.

    ``requests`` or ``requests[socks]`` do; ``requests<3`` and
    ``pkg @ git+https://…`` do not.

    :param str line: a requirement line, comment stripped
    :rtype: bool
    """
    try:
        req = Requirement(line)
    except InvalidRequirement:
        return False
    return not req.specifier and not req.url


# ── Source label ───────────────────────────────────────────────────────────

def _format_source(repo_url, repo_branch):
    """Return a short human-readable label for conflict markers."""
    if not repo_url:
        return 'unknown'
    m = re.search(r'github\.com[:/](.+)', (repo_url or '').rstrip('/'))
    name = m.group(1).removesuffix('.git') if m else repo_url.rstrip('/')
    return f'{name} ({repo_branch})' if repo_branch else name


# ── Conflict detection (field-based) ──────────────────────────────────────

def has_pip_conflicts(text):
    """True if pip_dependencies contains unresolved conflict markers."""
    return bool(text and '<<<<<<< ' in text)


def detect_pip_conflicts(text):
    """Parse conflict markers in pip_dependencies text.

    Returns a list of dicts:
        {name, existing, existing_source, incoming, incoming_source}
    """
    conflicts = []
    for m in CONFLICT_RE.finditer(text or ''):
        existing_line = m.group('existing_line').strip()
        parsed = _parse_req_line(existing_line)
        if parsed:
            conflicts.append({
                'name': parsed[0],
                'existing': existing_line,
                'existing_source': m.group('existing_src').strip(),
                'incoming': m.group('incoming_line').strip(),
                'incoming_source': m.group('incoming_src').strip(),
            })
    return conflicts


# ── Merge helpers ──────────────────────────────────────────────────────────

def _replace_whole_line(text, old_line, new_block):
    """Replace *old_line* as a complete line with *new_block*."""
    return re.sub(
        r'(?m)^' + re.escape(old_line) + r'$',
        new_block, text, count=1,
    )


def _replace_req_line(text, name, spec, new_block, keep_comment=False):
    """Replace the line of *text* that reads as *name* at *spec*.

    The line is found as :func:`_parse_req_line` reads it, so blanks
    around it or a trailing comment (``requests  # needed by X``) do not
    hide it, as they hid it from :func:`_replace_whole_line`: the text
    stayed as it was while the merge reported the line updated and a
    repo claimed it (9-nov-2026).

    :param str text: the requirements text
    :param str name: the package, lowercase
    :param str spec: the line's requirement, comment stripped
    :param str new_block: what the line becomes
    :param bool keep_comment: keep the line's trailing comment after
        *new_block* (not for a conflict block, whose last line is a
        source label)
    :return: ``(new_text, replaced)``
    :rtype: tuple
    """
    lines = text.split('\n')
    for index, line in enumerate(lines):
        if _parse_req_line(line) != (name, spec):
            continue
        comment = ''
        if keep_comment and '#' in line:
            comment = '  ' + line[line.index('#'):].strip()
        lines[index] = new_block + comment
        return '\n'.join(lines), True
    return text, False


def _build_pkg_map(text):
    """Parse *text* into ``{name: entry}``.

    Entry keys: ``type`` ('clean'|'conflict'), ``spec``, ``block``,
    ``existing_src`` (conflict only).
    """
    pkg_map = {}
    for m in CONFLICT_RE.finditer(text or ''):
        existing_line = m.group('existing_line').strip()
        parsed = _parse_req_line(existing_line)
        if parsed:
            pkg_map[parsed[0]] = {
                'type': 'conflict',
                'spec': parsed[1],
                'existing_src': m.group('existing_src').strip(),
                'block': m.group(0),
            }
    clean_only = CONFLICT_RE.sub('', text or '')
    for line in clean_only.split('\n'):
        parsed = _parse_req_line(line)
        if parsed and parsed[0] not in pkg_map:
            pkg_map[parsed[0]] = {
                'type': 'clean', 'spec': parsed[1], 'block': None,
            }
    return pkg_map


# ── Merge logic ────────────────────────────────────────────────────────────

def _clean_line_outcome(existing, incoming, entry, repo_key):
    """Decide what a repo's different line does to a clean stored line.

    :param str existing: the stored line
    :param str incoming: the line the repo brings
    :param dict entry: the package's provenance entry, or None
    :param str repo_key: the repo's provenance key, ``''`` outside
        provenance mode
    :return: ``'update'`` when the incoming line replaces the stored one
        (the repo owns the line, or its line is the stricter of two that
        agree and the stored line is a bare name), ``'kept'``
        when a person already turned the incoming line down,
        ``'satisfied'`` when the stored line already meets it, or
        ``None`` for a conflict
    :rtype: str or None

    A version or a range someone else wrote (the operator, or another
    repo) is not replaced by a stricter pin: the pin would make the line
    the repo's, and its next bump past that range (``foo<3`` replaced by
    ``foo==2.9``, then ``foo==3.0``) would go in without asking.
    """
    entry = entry or {}
    if repo_key and entry.get('repo') == repo_key:
        return 'update'
    if incoming in (entry.get('rejected') or ()):
        return 'kept'
    relation = pip_spec_relation(existing, incoming)
    if relation == 'existing':
        return 'satisfied'
    if relation == 'incoming' and _names_no_version(existing):
        return 'update'
    return None


def merge_pip_requirements(
    existing_text, incoming_text, repo_url=None, repo_branch=None,
    sources=None,
):
    """Merge *incoming_text* requirements into *existing_text*.

    - New packages are appended.
    - Exact duplicates are silently skipped.
    - Conflicts (same package, different spec) become git-style markers
      that show the repo/branch origin of each version.  If a conflict
      marker already exists for the package, only the incoming side and
      its source label are updated.

    Returns ``{'content': str, 'conflicts': list}`` where each conflict
    has ``{name, existing, existing_source, incoming, incoming_source}``.

    **Provenance mode.** Pass *sources* (a provenance map, possibly empty)
    to enable per-package authority. Repo lines float to the tip of their
    branch on every rebuild, so upstream keeps editing its own
    ``requirements.txt``; without authority every such edit would surface
    as a conflict a human has to click through, even though nobody
    disagrees. With a map:

    - a package whose recorded owner is *this* repo is **updated in
      place** (upstream maintaining its own dependency), and
    - a package owned by the operator or by another repo still becomes a
      conflict marker — that is a real disagreement.

    Absence of an entry means "the operator owns this line", so an empty
    map degrades to the historical behaviour and no migration is needed:
    ownership is claimed lazily the first time a repo declares a spec
    that already matches verbatim. An entry whose ``repo`` is empty is
    the operator's too, written when a person resolved a conflict.

    Two different lines of one package are a conflict only when they
    cannot both hold (:func:`pip_spec_relation`): a line the stored one
    already meets is skipped (``satisfied``), and a stricter one that
    meets the stored line replaces it (``updated``), as pip would have
    to satisfy both anyway, when the stored line is a bare name; a
    version or range the operator or another repo wrote is asked about. A
    stored line the text no longer holds as one line is asked about
    too, rather than reported updated. A line a person already turned
    down when resolving a conflict on that package (the entry's
    ``rejected``) is skipped too (``kept``): the same disagreement is
    not asked twice.

    The map is keyed by lowercase package name; each entry is
    ``{'repo': <normalized url>, 'spec': <raw line>, 'label': <source>}``
    plus, after a resolution, ``'rejected': [<raw line>, ...]``.
    In provenance mode the result also carries ``sources`` (the updated
    map) plus ``added`` / ``updated`` / ``adopted`` / ``removed`` for the
    caller's log — ``removed`` lists this repo's own packages that no
    longer appear in its manifest; they are never deleted from the text,
    only reported so the job log leaves a trace. ``satisfied`` and
    ``kept`` list ``{name, existing, incoming}`` in every mode.
    """
    track_sources = sources is not None
    src_map = dict(sources or {})
    repo_key = _normalize_url(repo_url) if repo_url else ''
    source_label = _format_source(repo_url, repo_branch)
    pkg_map = _build_pkg_map(existing_text)
    new_text = existing_text or ''
    added = []
    updated = []
    adopted = []
    satisfied = []
    kept = []
    conflicts = []
    incoming_names = set()

    def _claim(name, spec):
        """Record *this* repo as the author of *name*'s current spec.

        The lines a person turned down on this package stay turned down,
        but for *spec*: the line installed is no longer a refused one.
        """
        entry = {'repo': repo_key, 'spec': spec, 'label': source_label}
        rejected = [
            line for line in (src_map.get(name) or {}).get('rejected') or ()
            if line != spec
        ]
        if rejected:
            entry['rejected'] = rejected
        src_map[name] = entry

    for line in (incoming_text or '').split('\n'):
        parsed = _parse_req_line(line)
        if not parsed:
            continue
        name, raw = parsed
        incoming_names.add(name)

        if name not in pkg_map:
            added.append(raw)
            pkg_map[name] = {'type': 'clean', 'spec': raw, 'block': None}
            if track_sources and repo_key:
                _claim(name, raw)

        elif pkg_map[name]['spec'] == raw:
            # Exact match. Claim it when nobody owns it yet so a later
            # upstream bump reads as maintenance instead of a conflict.
            # Only for clean lines: a package sitting in an unresolved
            # conflict block has no settled owner to record.
            if (track_sources and repo_key
                    and pkg_map[name]['type'] == 'clean'
                    and name not in src_map):
                _claim(name, raw)
                adopted.append(name)

        elif pkg_map[name]['type'] == 'conflict':
            # Update the incoming side of the existing conflict block
            old_block = pkg_map[name]['block']
            existing_src = pkg_map[name]['existing_src']
            existing_spec = pkg_map[name]['spec']
            new_block = (
                f'<<<<<<< {existing_src}\n'
                f'{existing_spec}\n'
                f'=======\n'
                f'{raw}\n'
                f'>>>>>>> {source_label}'
            )
            new_text = new_text.replace(old_block, new_block, 1)
            pkg_map[name]['block'] = new_block
            conflicts.append({
                'name': name,
                'existing': existing_spec,
                'existing_source': existing_src,
                'incoming': raw,
                'incoming_source': source_label,
            })

        elif (outcome := _clean_line_outcome(
                pkg_map[name]['spec'], raw, src_map.get(name),
                repo_key if track_sources else '')) in ('kept', 'satisfied'):
            # Turned down before, or already met: nothing to ask.
            (kept if outcome == 'kept' else satisfied).append({
                'name': name, 'existing': pkg_map[name]['spec'],
                'incoming': raw,
            })

        elif outcome == 'update' and (replaced := _replace_req_line(
                new_text, name, pkg_map[name]['spec'], raw,
                keep_comment=True))[1]:
            # This repo wrote the line and is now changing it (upstream
            # maintaining its own dependency), or it asks for a stricter
            # line that meets the stored one: pip has to meet both, so
            # the line that does is the one to install.
            old_spec = pkg_map[name]['spec']
            new_text = replaced[0]
            pkg_map[name] = {'type': 'clean', 'spec': raw, 'block': None}
            if track_sources and repo_key:
                _claim(name, raw)
            updated.append({'name': name, 'old': old_spec, 'new': raw})

        else:
            # Clean line → introduce a new conflict marker
            existing_spec = pkg_map[name]['spec']
            new_block = (
                f'<<<<<<< existing\n'
                f'{existing_spec}\n'
                f'=======\n'
                f'{raw}\n'
                f'>>>>>>> {source_label}'
            )
            new_text, found = _replace_req_line(
                new_text, name, existing_spec, new_block,
            )
            if not found:
                # Still a conflict the job stops on, but with no markers
                # for the dialog to resolve: worth a trace.
                _logger.warning(
                    "pip merge: no line reads as %r for %s's conflict "
                    "markers", existing_spec, name,
                )
            pkg_map[name] = {
                'type': 'conflict',
                'spec': existing_spec,
                'existing_src': 'existing',
                'block': new_block,
            }
            conflicts.append({
                'name': name,
                'existing': existing_spec,
                'existing_source': 'existing',
                'incoming': raw,
                'incoming_source': source_label,
            })

    if added:
        base = new_text.rstrip('\n')
        sep = '\n' if base else ''
        new_text = base + sep + '\n'.join(added)

    # A package this repo owns that vanished from its manifest is kept —
    # dropping a line silently could break a build that still needs it —
    # but reported, so the job log records that upstream let it go.
    removed = []
    if track_sources and repo_key:
        removed = sorted(
            name for name, entry in src_map.items()
            if (entry or {}).get('repo') == repo_key
            and name not in incoming_names
        )

    result = {
        'content': new_text,
        'conflicts': conflicts,
        'added': added,
        'updated': updated,
        'adopted': adopted,
        'removed': removed,
        'satisfied': satisfied,
        'kept': kept,
    }
    # ``sources`` only in provenance mode: a caller that did not pass a
    # map must not be able to persist an empty one and silently wipe the
    # provenance of every package.
    if track_sources:
        result['sources'] = src_map
    return result


def prune_pip_sources(text, sources):
    """Return *sources* without entries that *text* no longer backs.

    Provenance is only meaningful while the recorded spec is still in the
    field verbatim. When someone edits ``pip_dependencies`` by hand —
    changing a version, deleting a line, resolving a marker — that line
    becomes theirs again, and the next re-sync must treat an upstream
    change to it as a conflict rather than overwrite the edit.

    A package still in its conflict markers, with its recorded spec as
    the stored side, keeps its entry: an edit of another line answered
    nothing about it, and dropping the entry lost its owner and the
    lines turned down before (9-nov-2026).

    :param text: the current ``pip_dependencies`` content
    :param sources: the provenance map to filter
    :returns: a new map holding only entries still backed by a clean
        line, or by the stored side of the package's conflict block
    """
    if not sources:
        return {}
    pkg_map = _build_pkg_map(text)
    return {
        name: entry
        for name, entry in sources.items()
        if name in pkg_map
        and pkg_map[name]['spec'] == (entry or {}).get('spec')
    }


def apply_requirements_content(owner, content, repo_url, repo_branch):
    """Merge already-fetched requirements into a project or instance.

    This is the no-network counterpart of a repo line's normal
    ``_apply_requirements`` hook.  Import inspection can fetch the file under
    its shared HTTP budget, create the repo with that hook disabled and then
    apply exactly those bytes without a second request.

    :param owner: ``cloud.project`` or ``cloud.instance`` singleton
    :param str content: decoded ``requirements.txt`` content
    :param str repo_url: source repository URL used for provenance
    :param str repo_branch: source branch used for provenance
    :return: merge result, or ``None`` when content is empty
    """
    if not content:
        return None
    from ._pip_apt_map import resolve_apt_dependencies

    old_sources = owner.pip_dependency_sources or {}
    result = merge_pip_requirements(
        owner.pip_dependencies,
        content,
        repo_url=repo_url,
        repo_branch=repo_branch,
        sources=old_sources,
    )
    values = {}
    if result["content"] != (owner.pip_dependencies or ""):
        values["pip_dependencies"] = result["content"]
        needed = resolve_apt_dependencies(result["content"])
        if needed:
            existing = set((owner.apt_dependencies or "").split())
            new_apt = needed - existing
            if new_apt:
                values["apt_dependencies"] = (
                    (owner.apt_dependencies or "").rstrip()
                    + "\n"
                    + "\n".join(sorted(new_apt))
                ).strip()
    if result["sources"] != old_sources:
        values["pip_dependency_sources"] = result["sources"]
    if values:
        owner.with_context(pip_provenance_managed=True).write(values)
    return result


def repo_key_for_source_label(repos, label):
    """Return the provenance key of the repo line matching *label*.

    Conflict markers carry the human label produced by
    :func:`_format_source`, not a URL. When a user resolves a conflict in
    favour of the incoming side, ownership has to go back to the repo
    that proposed it — this maps the label to that repo's key.

    :param repos: recordset of repo lines to search
    :param label: source label as stored in the conflict marker
    :returns: the normalized repo URL, or ``''`` when no line matches
    """
    wanted = (label or '').strip()
    for repo in repos:
        if _format_source(repo.url, repo.branch) == wanted:
            return _normalize_url(repo.url)
    return ''


def resolved_pip_source(repos, match, chosen, previous):
    """Return the provenance entry a person's answer to one conflict leaves.

    The answer is remembered: the side not chosen goes to the entry's
    ``rejected`` lines, which the next re-sync skips instead of asking
    again (:func:`merge_pip_requirements`). Before, siding with the
    stored line dropped the entry, the repo that wrote that line adopted
    it again, and the other repo's line raised the same conflict at
    every deploy (OCA/queue against core 18.0's ``requests`` pin,
    9-nov-2026).

    Siding with the repo's line hands it to that repo, so its next bump
    applies on its own. Keeping the stored line keeps its owner: a
    repo's line stays that repo's, so its next bump still needs no
    person, and an unowned line becomes the operator's. A third line
    typed by hand is the operator's (``repo`` empty): no repo adopts it
    again, and an upstream change to it is asked about.

    A line stays turned down until someone edits the package's line by
    hand outside a conflict (the edit drops the entry,
    :func:`prune_pip_sources`) or its owner moves the line to it
    (``_claim`` in :func:`merge_pip_requirements`).

    :param repos: the parent's repo lines, to map labels to repos
    :param match: the ``CONFLICT_RE`` match of the resolved block
    :param str chosen: the line the person kept, stripped
    :param dict previous: the package's entry before the answer, or None
    :return: the new entry for the package
    :rtype: dict
    """
    existing = match.group('existing_line').strip()
    incoming = match.group('incoming_line').strip()
    previous = previous or {}
    rejected = list(previous.get('rejected') or [])
    for line in (existing, incoming):
        if line != chosen and line not in rejected:
            rejected.append(line)
    rejected = [line for line in rejected if line != chosen]
    repo_key, label = '', 'operator'
    if chosen == incoming:
        repo_key = repo_key_for_source_label(repos, match.group('incoming_src'))
        label = match.group('incoming_src').strip()
    elif chosen == previous.get('spec') and previous.get('repo'):
        repo_key, label = previous['repo'], previous.get('label') or label
    entry = {
        'repo': repo_key,
        'spec': chosen,
        'label': label if repo_key else 'operator',
    }
    if rejected:
        entry['rejected'] = rejected
    return entry


def hand_resolved_pip_sources(old_text, new_text, repos, sources):
    """Return *sources* with the conflicts a hand edit answered remembered.

    «Edit Manually» opens the list and the person deletes the markers
    themselves. A conflict block that was in *old_text* and is gone from
    *new_text*, leaving one of its two lines, is answered as the dialog
    would answer it (:func:`resolved_pip_source`): the other line is
    turned down and not asked again at the next re-sync. A third line
    typed instead is no answer to remember, nor is an edit that leaves
    both lines (only the markers deleted) or any piece of a marker:
    the person has not chosen yet.

    :param str old_text: ``pip_dependencies`` before the edit
    :param str new_text: ``pip_dependencies`` after it
    :param repos: the parent's repo lines, to map labels to repos
    :param dict sources: the provenance map before the edit
    :return: a new map, with the answered packages' entries replaced
    :rtype: dict
    """
    answered = dict(sources or {})
    if not has_pip_conflicts(old_text):
        return answered
    outside_blocks = CONFLICT_RE.sub('', new_text or '').split('\n')
    if any(line.startswith(('<<<<<<<', '=======', '>>>>>>>'))
           for line in outside_blocks):
        return answered
    lines_of = {}
    for line in outside_blocks:
        parsed = _parse_req_line(line)
        if parsed:
            lines_of[parsed[0]] = lines_of.get(parsed[0], 0) + 1
    pkg_map = _build_pkg_map(new_text)
    for match in CONFLICT_RE.finditer(old_text):
        parsed = _parse_req_line(match.group('existing_line'))
        if not parsed:
            continue
        name = parsed[0]
        line = pkg_map.get(name) or {}
        if line.get('type') != 'clean' or lines_of.get(name) != 1:
            continue
        sides = (
            match.group('existing_line').strip(),
            match.group('incoming_line').strip(),
        )
        if line['spec'] in sides:
            answered[name] = resolved_pip_source(
                repos, match, line['spec'], answered.get(name),
            )
    return answered


# ── Alert helper ───────────────────────────────────────────────────────────

def create_pip_conflict_alert(
    env, conflicts, instance_id=None, project_id=None, job_id=None,
):
    """Raise or refresh the pip_conflict alert for the given target.

    :param job_id: the job that found the conflicts, when one did; its
        failure is then this alert's news and raises no ``job_failed``
    :return: the alert, or None when there is nothing to raise
    """
    if not conflicts or (not instance_id and not project_id):
        return None
    Alert = env['cloud.alert']
    names = ', '.join(c['name'] for c in conflicts[:3])
    if len(conflicts) > 3:
        names += f' (+{len(conflicts) - 3} more)'
    target = (
        {'instance': env['cloud.instance'].browse(instance_id)}
        if instance_id
        else Alert._link_target('project_id', project_id)
    )
    alert = Alert.raise_alert(
        'pip_conflict',
        f"Pip dependency conflicts: {names}",
        job=env['cloud.job'].browse(job_id) if job_id else None,
        extra={'conflict_data': conflicts},
        **target,
    )
    # Empty when a concurrent producer filed it first: the job this
    # blocks must point at that one, not at nothing.
    return alert or Alert.search(
        Alert._dedup_domain('pip_conflict', **target), limit=1,
    )


# ── GitHub fetch ───────────────────────────────────────────────────────────

def fetch_repo_addons(env, url, branch):
    """Fetch Odoo addon module names found at the root of a GitHub repo branch.

    Uses the recursive tree API to find dirs with ``__manifest__.py``.
    Tries: PAT → GitHub App → unauthenticated. A repository of a Git
    provider (GitLab) is read with the panel's git instead
    (:func:`git_source_addons`).
    Returns a sorted list of module names, or ``None`` on failure.
    """
    on_provider, found = _provider_read(env, url, git_source_addons, branch)
    if on_provider:
        return found
    try:
        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError:
            return None

        def _get_addons(client):
            tree = client.get(
                f'/repos/{owner}/{repo_name}'
                f'/git/trees/{branch}?recursive=1'
            )
            # Extract addon name (parent dir of __manifest__.py)
            # at any depth: root/mod/__manifest__.py or mod/__manifest__.py
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in tree.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })

        svc = env['cloud.github.credential.service'].sudo()

        with suppress(GitHubAPIError, ValueError, UserError, Exception):
            pat = svc.get_pat()
            if pat:
                return _get_addons(GitHubPATClient(pat))

        with suppress(GitHubAPIError, ValueError, UserError, Exception):
            return _get_addons(GitHubAppClient(svc.get_credentials()))

        with suppress(Exception):
            api_url = (
                f'https://api.github.com/repos/{owner}/{repo_name}'
                f'/git/trees/{branch}?recursive=1'
            )
            req = urllib.request.Request(api_url, headers={
                'User-Agent': 'incubacloud/1.0',
                'Accept': 'application/vnd.github+json',
            })
            with safe_urlopen(req, timeout=4) as resp:  # nosec B310 — hardcoded https://api.github.com
                data = json.loads(resp.read().decode())
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in data.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })

        return None

    except Exception:
        _logger.debug(
            "Unexpected error fetching addons for %s@%s",
            url, branch, exc_info=True,
        )
        return None


def fetch_requirements_txt(env, url, branch, deadline=None):
    """Fetch *requirements.txt* from the root of a repository branch.

    A repository of a Git provider (GitLab) is read with the panel's
    git (:func:`git_source_requirements`), before *deadline* when one
    is given (a request's :class:`GitReadBudget`); a GitHub one through
    its API (:func:`_fetch_github_requirements_txt`).

    :param env: an Odoo environment
    :param str url: the repository URL, of GitHub or of a Git provider
    :param str branch: the branch
    :param float deadline: the request's (:class:`GitReadBudget`), or None
    :return: the file's content, or None when it is missing or the read
        fails
    :rtype: str or None
    """
    on_provider, found = _provider_read(
        env, url, git_source_requirements, branch, deadline=deadline,
    )
    if on_provider:
        return found
    return _fetch_github_requirements_txt(env, url, branch)


def _fetch_github_requirements_txt(env, url, branch):
    """Fetch *requirements.txt* from the root of a GitHub repo branch.

    Tries: PAT → GitHub App → unauthenticated.

    :param env: an Odoo environment
    :param str url: the GitHub repository URL
    :param str branch: the branch
    :return: the file's content, or None when it is missing or the read
        fails
    :rtype: str or None
    """
    try:
        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError:
            return None

        def _decode(client):
            data = client.get(
                f'/repos/{owner}/{repo_name}'
                f'/contents/requirements.txt?ref={branch}'
            )
            return base64.b64decode(
                data.get('content', '').replace('\n', '').replace(' ', '')
            ).decode('utf-8')

        svc = env['cloud.github.credential.service'].sudo()

        # 1. PAT
        try:
            pat = svc.get_pat()
            if pat:
                return _decode(GitHubPATClient(pat))
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return None
        except (ValueError, UserError):
            _logger.debug("fetch_requirements_txt PAT not configured", exc_info=True)
        except Exception:
            _logger.debug("fetch_requirements_txt PAT unexpected error", exc_info=True)

        # 2. GitHub App
        try:
            return _decode(GitHubAppClient(svc.get_credentials()))
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return None
        except (ValueError, UserError):
            _logger.debug("fetch_requirements_txt App not configured", exc_info=True)
        except Exception:
            _logger.debug("fetch_requirements_txt App unexpected error", exc_info=True)

        # 3. Unauthenticated fallback (public repos)
        try:
            api_url = (
                f'https://api.github.com/repos/{owner}/{repo_name}'
                f'/contents/requirements.txt?ref={branch}'
            )
            req = urllib.request.Request(api_url, headers={
                'User-Agent': 'incubacloud/1.0',
                'Accept': 'application/vnd.github+json',
            })
            with safe_urlopen(req, timeout=4) as resp:  # nosec B310 — hardcoded https://api.github.com
                data = json.loads(resp.read().decode())
                content = data.get('content', '')
                raw = content.replace('\n', '').replace(' ', '')
                return base64.b64decode(raw).decode('utf-8')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
        except Exception:
            _logger.debug("fetch_requirements_txt unauthenticated fallback error", exc_info=True)

        return None

    except Exception:
        _logger.debug(
            "Unexpected error fetching requirements.txt for %s@%s",
            url, branch, exc_info=True,
        )
        return None


def resolve_github_client(env):
    """Return a GitHub API client, resolved on the CALLING thread.

    Credential lookup touches the ORM, which is not thread-safe, so an
    executor resolves the client first and hands it to the worker thread
    that does the HTTP. Same split as ``detect_addon_conflicts``.

    :returns: a client instance, or ``None`` when no credentials are
        configured (callers then fall back to unauthenticated requests).
    """
    client = None
    svc = env['cloud.github.credential.service'].sudo()
    with suppress(Exception):
        pat = svc.get_pat()
        if pat:
            client = GitHubPATClient(pat)
    if client is None:
        with suppress(Exception):
            client = GitHubAppClient(svc.get_credentials())
    return client


def fetch_requirements_txt_with_client(client, url, branch):
    """Fetch ``requirements.txt`` without touching the ORM.

    Thread-safe counterpart of :func:`fetch_requirements_txt`: takes the
    client resolved by :func:`resolve_github_client` instead of an env,
    so it can run inside ``asyncio.to_thread``.

    :param client: pre-resolved GitHub client, or None; for a GitLab
        repository, its :data:`GitSource`
    :param url: repository URL
    :param branch: branch to read the file from
    :returns: file content as str, or ``None`` when it cannot be read
        (missing file, private repo without credentials, network error)
    """
    if isinstance(client, GitSource):
        with suppress(GitReaderError, ValueError):
            return git_source_requirements(client, branch)
        return None
    try:
        owner, repo_name = _parse_github_repo_path(url)
    except ValueError:
        return None
    path = (
        f'/repos/{owner}/{repo_name}'
        f'/contents/requirements.txt?ref={branch}'
    )

    def _decode(payload):
        raw = (payload.get('content') or '')
        raw = raw.replace('\n', '').replace(' ', '')
        return base64.b64decode(raw).decode('utf-8')

    if client:
        try:
            return _decode(client.get(path))
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return None
        except Exception:
            _logger.debug(
                "requirements.txt fetch failed for %s@%s (client)",
                url, branch, exc_info=True,
            )

    req = urllib.request.Request(
        f'https://api.github.com{path}',
        headers={
            'User-Agent': 'incubacloud/1.0',
            'Accept': 'application/vnd.github+json',
        },
    )
    try:
        with safe_urlopen(req, timeout=4) as resp:  # nosec B310 — hardcoded https://api.github.com
            return _decode(json.loads(resp.read().decode()))
    except Exception:
        _logger.debug(
            "requirements.txt fetch failed for %s@%s (anonymous)",
            url, branch, exc_info=True,
        )
        return None


# ── Pre-flight addon conflict detection ───────────────────────────────────

# ── Instance ↔ Project comparison ──────────────────────────────────────

def _normalize_url(url):
    """Normalize a repo URL for comparison (strip, lowercase, remove .git)."""
    return (url or '').strip().rstrip('/').removesuffix('.git').lower()


def _parse_dep_lines(text):
    """Parse dependency text into {normalized_key: raw_line} map.

    For pip: key = lowercase package name (via _parse_req_line).
    For apt: key = stripped lowercase line.
    Skips empty lines, comments, and conflict marker blocks.
    """
    # Strip conflict markers first
    clean = CONFLICT_RE.sub('', text or '')
    result = {}
    for line in clean.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        parsed = _parse_req_line(stripped)
        if parsed:
            result[parsed[0]] = stripped
        else:
            # Non-pip line (apt, or git+ URL without version)
            result[stripped.lower()] = stripped
    return result


def compare_instance_project(instance):
    """Compare instance deps/repos with its project template.

    Returns a dict with ``pip``, ``apt``, ``repos`` sections.
    Each section has: ``only_instance``, ``only_project``,
    ``conflicts``, ``common`` lists.

    Raises ValueError if either side has unresolved pip conflict markers.
    """
    project = instance.project_id
    if not project:
        raise ValueError("Instance has no project")

    if has_pip_conflicts(instance.pip_dependencies):
        raise ValueError(
            "Instance has unresolved pip conflicts. "
            "Resolve them before syncing."
        )
    if has_pip_conflicts(project.pip_dependencies):
        raise ValueError(
            "Project has unresolved pip conflicts. "
            "Resolve them before syncing."
        )

    pip_diff = _compare_deps(
        instance.pip_dependencies or '',
        project.pip_dependencies or '',
    )
    apt_diff = _compare_deps(
        instance.apt_dependencies or '',
        project.apt_dependencies or '',
    )
    repos_diff = _compare_repos(instance.repo_ids, project.repo_ids)

    return {
        'pip': pip_diff,
        'apt': apt_diff,
        'repos': repos_diff,
    }


def _compare_deps(instance_text, project_text):
    """Compare two dependency texts line by line."""
    inst_map = _parse_dep_lines(instance_text)
    proj_map = _parse_dep_lines(project_text)

    only_instance, only_project, conflicts, common = [], [], [], []

    for key in sorted(set(inst_map) | set(proj_map)):
        in_inst = key in inst_map
        in_proj = key in proj_map
        if in_inst and in_proj:
            if inst_map[key] == proj_map[key]:
                common.append(inst_map[key])
            else:
                conflicts.append({
                    'name': key,
                    'instance_line': inst_map[key],
                    'project_line': proj_map[key],
                })
        elif in_inst:
            only_instance.append(inst_map[key])
        else:
            only_project.append(proj_map[key])

    return {
        'only_instance': only_instance,
        'only_project': only_project,
        'conflicts': conflicts,
        'common': common,
    }


def _compare_repos(inst_repos, proj_repos):
    """Compare instance repos with project repos by normalized URL."""
    inst_map = {_normalize_url(r.url): r for r in inst_repos}
    proj_map = {_normalize_url(r.url): r for r in proj_repos}

    only_instance, only_project, conflicts, common = [], [], [], []

    for key in sorted(set(inst_map) | set(proj_map)):
        in_inst = key in inst_map
        in_proj = key in proj_map
        if in_inst and in_proj:
            ir, pr = inst_map[key], proj_map[key]
            diffs = {}
            for field in ('branch', 'addons', 'excludes', 'commit_sha'):
                iv = (getattr(ir, field) or '').strip()
                pv = (getattr(pr, field) or '').strip()
                if iv != pv:
                    diffs[field] = {'instance': iv, 'project': pv}
            if diffs:
                conflicts.append({
                    'url': ir.url,
                    'fields': diffs,
                })
            else:
                common.append(ir.url)
        elif in_inst:
            ir = inst_map[key]
            only_instance.append({
                'url': ir.url,
                'branch': ir.branch or 'main',
                'addons': ir.addons or '',
                'excludes': ir.excludes or '',
                'commit_sha': ir.commit_sha or '',
            })
        else:
            pr = proj_map[key]
            only_project.append({
                'url': pr.url,
                'branch': pr.branch or 'main',
                'addons': pr.addons or '',
                'excludes': pr.excludes or '',
                'commit_sha': pr.commit_sha or '',
            })

    return {
        'only_instance': only_instance,
        'only_project': only_project,
        'conflicts': conflicts,
        'common': common,
    }


def apply_sync(instance, actions):
    """Apply user-selected sync actions between instance and project.

    ``actions`` is a dict with keys: ``pip``, ``apt``, ``repos``.
    Each has ``push`` (list of lines/urls to copy instance→project),
    ``pull`` (list to copy project→instance), and ``conflicts``
    (dict of {name: 'instance'|'project'} for conflict resolution).
    """
    project = instance.project_id

    # ── Dependencies ──────────────────────────────────────────────────
    for dep_field in ('pip', 'apt'):
        field_name = f'{dep_field}_dependencies'
        dep_actions = actions.get(dep_field, {})

        push_lines = dep_actions.get('push', [])
        pull_lines = dep_actions.get('pull', [])
        conflict_choices = dep_actions.get('conflicts', {})

        if push_lines:
            current = (getattr(project, field_name) or '').rstrip('\n')
            sep = '\n' if current else ''
            setattr(project, field_name, current + sep + '\n'.join(push_lines))

        if pull_lines:
            current = (getattr(instance, field_name) or '').rstrip('\n')
            sep = '\n' if current else ''
            setattr(instance, field_name, current + sep + '\n'.join(pull_lines))

        if conflict_choices:
            # For each conflict, the user chose which side wins.
            # Replace the line on the losing side with the winning line.
            inst_map = _parse_dep_lines(getattr(instance, field_name) or '')
            proj_map = _parse_dep_lines(getattr(project, field_name) or '')
            for name, winner in conflict_choices.items():
                if winner == 'instance' and name in inst_map and name in proj_map:
                    # Push instance line to project
                    proj_text = getattr(project, field_name) or ''
                    proj_text = _replace_whole_line(
                        proj_text, proj_map[name], inst_map[name],
                    )
                    setattr(project, field_name, proj_text)
                elif winner == 'project' and name in proj_map and name in inst_map:
                    # Pull project line to instance
                    inst_text = getattr(instance, field_name) or ''
                    inst_text = _replace_whole_line(
                        inst_text, inst_map[name], proj_map[name],
                    )
                    setattr(instance, field_name, inst_text)

    # ── Repos ─────────────────────────────────────────────────────────
    repo_actions = actions.get('repos', {})
    push_repos = repo_actions.get('push', [])
    pull_repos = repo_actions.get('pull', [])
    repo_conflicts = repo_actions.get('conflicts', {})

    ProjectRepo = instance.env['cloud.project.repo'].with_context(
        skip_apply_requirements=True,
    )
    InstanceRepo = instance.env['cloud.instance.repo'].with_context(
        skip_apply_requirements=True,
    )

    # Push repos from instance to project
    for url in push_repos:
        norm = _normalize_url(url)
        src = next(
            (r for r in instance.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        if src:
            ProjectRepo.create({
                'project_id': project.id,
                'url': src.url,
                'branch': src.branch or 'main',
                'addons': src.addons or '',
                'excludes': src.excludes or '',
                'commit_sha': src.commit_sha or '',
            })

    # Pull repos from project to instance
    for url in pull_repos:
        norm = _normalize_url(url)
        src = next(
            (r for r in project.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        if src:
            InstanceRepo.create({
                'instance_id': instance.id,
                'url': src.url,
                'branch': src.branch or 'main',
                'addons': src.addons or '',
                'excludes': src.excludes or '',
                'commit_sha': src.commit_sha or '',
            })

    # Resolve repo conflicts (update losing side with winning values)
    for url, winner in repo_conflicts.items():
        norm = _normalize_url(url)
        inst_repo = next(
            (r for r in instance.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        proj_repo = next(
            (r for r in project.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        if not inst_repo or not proj_repo:
            continue
        if winner == 'instance':
            proj_repo.with_context(skip_apply_requirements=True).write({
                'branch': inst_repo.branch,
                'addons': inst_repo.addons or '',
                'excludes': inst_repo.excludes or '',
                'commit_sha': inst_repo.commit_sha or '',
            })
        else:
            inst_repo.with_context(skip_apply_requirements=True).write({
                'branch': proj_repo.branch,
                'addons': proj_repo.addons or '',
                'excludes': proj_repo.excludes or '',
                'commit_sha': proj_repo.commit_sha or '',
            })


# GitHub cuts a recursive tree at 7 MB; one answer is read at most this.
_ADDON_TREE_BYTES = 8 * 1024 * 1024


def _listed_modules(repo):
    """Return the modules a line names itself, or None when it lists all.

    :param repo: a repository line
    :return: its ``addons`` less its ``excludes``; None for a wildcard,
        whose modules only a read of the repository says
    :rtype: set or None
    """
    addons_str = (repo.addons or "").strip()
    if not addons_str:
        return None
    modules = _parse_repo_addons(addons_str)
    excludes_str = (repo.excludes or "").strip()
    if excludes_str:
        modules = _apply_excludes(modules, excludes_str)
    if modules == ["*"]:
        return None
    return set(modules)


async def detect_addon_conflicts(
    env, repos, unread=None, budget=None, git_budget=None,
):
    """Fetch module lists from all repos and find cross-repo duplicates.

    For each repo, resolves its effective module set by applying the
    ``addons`` and ``excludes`` fields.  When the repo uses a wildcard
    (empty ``addons``), the full module list is fetched from GitHub.

    A wildcard repo GitHub would not list is left out of the comparison,
    so "no conflict" then only speaks for the repos that were read.

    :param list unread: when given, the aliases of the repos that could
        not be read are appended to it
    :param budget: an ``HTTPBudget`` every GitHub request is charged to
        and timed against; a repository it leaves no room for is unread
    :param GitReadBudget git_budget: a panel request's: each GitLab
        repository read takes one of its reads, and one it has none left
        for is unread. None in the deploy, which reads them all.
    :return: a list of ``{'addon': str, 'repos': [alias, ...]}``
    """
    repo_modules: dict[str, set[str]] = {}

    # Resolve credentials in the main thread (env is not thread-safe)
    _client = None
    svc = env['cloud.github.credential.service'].sudo()
    with suppress(Exception):
        pat = svc.get_pat()
        if pat:
            _client = GitHubPATClient(pat)
    if _client is None:
        with suppress(Exception):
            _client = GitHubAppClient(svc.get_credentials())

    # GitLab repositories: their reader and credential, from the ORM too,
    # and here the reads the request has room for.
    sources = _git_sources(env, repos)
    no_room = set()
    if git_budget is not None:
        for repo in repos:
            if repo.url not in sources or _listed_modules(repo) is not None:
                continue
            if git_budget.take():
                sources[repo.url].reader.deadline = git_budget.deadline
            else:
                no_room.add(repo)

    def _fetch_addons_with_client(url, branch):
        """Thread-safe: uses pre-resolved client, no env access."""
        if url in sources:
            return _source_addons_or_none(sources[url], branch)
        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError:
            return None
        def _get(client):
            tree = client.get(
                f'/repos/{owner}/{repo_name}'
                f'/git/trees/{branch}?recursive=1',
                budget=budget,
                max_bytes=_ADDON_TREE_BYTES if budget is not None else None,
            )
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in tree.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })
        if _client:
            with suppress(Exception):
                return _get(_client)
        # Public fallback
        api_url = (
            f'https://api.github.com/repos/{owner}/{repo_name}'
            f'/git/trees/{branch}?recursive=1'
        )
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'incubacloud/1.0',
            'Accept': 'application/vnd.github+json',
        })
        with suppress(Exception):
            timeout = 4
            if budget is not None:
                budget.begin_request()
                timeout = budget.remaining_timeout(timeout)
            with safe_urlopen(req, timeout=timeout) as resp:  # nosec B310 — hardcoded https://api.github.com
                if budget is not None:
                    data = read_json_limited(
                        resp, budget=budget, max_bytes=_ADDON_TREE_BYTES,
                    )
                else:
                    data = json.loads(resp.read().decode())
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in data.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })
        return None

    async def _fetch_one(repo):
        alias = _repo_alias(repo.url)
        if not alias:
            return
        excludes_str = (repo.excludes or "").strip()
        listed = _listed_modules(repo)
        if listed is not None:
            repo_modules[alias] = listed
            return
        if repo in no_room:
            _logger.info(
                "Addon check: %s@%s not read: git read limit of the request",
                alias, repo.branch,
            )
            if unread is not None:
                unread.append(alias)
            return

        # Wildcard: fetch full list from GitHub
        all_mods = await asyncio.to_thread(
            _fetch_addons_with_client,
            repo.url, repo.branch or "main",
        )
        if all_mods is None:
            _logger.warning(
                "Addon check: could not fetch addons for %s@%s",
                alias, repo.branch,
            )
            if unread is not None:
                unread.append(alias)
            return

        _logger.info(
            "Addon check: %s@%s → %d addons",
            alias, repo.branch, len(all_mods),
        )
        if excludes_str:
            filtered = _apply_excludes(all_mods, excludes_str)
            repo_modules[alias] = (
                set(filtered) if filtered != ["*"]
                else set(all_mods)
            )
        else:
            repo_modules[alias] = set(all_mods)

    await asyncio.gather(*(_fetch_one(r) for r in repos))
    _logger.info(
        "Addon check: %d repos, modules: %s",
        len(repo_modules),
        {k: len(v) for k, v in repo_modules.items()},
    )

    # Cross-reference: addons present in 2+ repos
    addon_to_repos: dict[str, list[str]] = {}
    for alias, modules in repo_modules.items():
        for mod in modules:
            addon_to_repos.setdefault(mod, []).append(alias)

    return [
        {"addon": addon, "repos": sorted(repos_list)}
        for addon, repos_list in sorted(addon_to_repos.items())
        if len(repos_list) > 1
    ]
