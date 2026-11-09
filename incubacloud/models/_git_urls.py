"""Repository URLs of a Git provider other than GitHub (GitLab), without the ORM.

A repository line names a GitLab repository by any spelling a user
copies from GitLab: https, http, no scheme, ``git@host:`` or
``ssh://git@host[:port]/``. The panel keeps none of them as typed: it
finds the provider whose ``base_url`` has exactly the same authority and
path prefix, checks the rest is a project path as GitLab names them, and
rebuilds the URL from the provider's base. That rebuilt URL is the only
one a token ever travels with (8-nov-2026).

The URL always ends in ``.git``: without it gitlab.com answers a clone
with a 301 to the ``.git`` form, and the panel follows no redirect, so
the clone would fail.
"""
import re
from collections import namedtuple
from urllib.parse import urlsplit

#: A repository of a provider, as the panel keeps it.
#:
#: ``kind``: ``'gitlab'`` (or ``'github'`` from
#: :func:`.._repo_requirements.resolve_repo_ref`); ``base``: the
#: provider's canonical ``base_url``; ``authority``: ``host[:port]``;
#: ``path``: the project path in lower case, the key lines and
#: credentials match on; ``canonical``: the https URL rebuilt from the
#: base, with the path as it was spelled and ``.git`` at the end.
RepoRef = namedtuple("RepoRef", "kind base authority path canonical")

#: One segment of a GitLab path: a letter, digit or ``_`` first, then
#: letters, digits, ``_``, ``.`` and ``-`` (GitLab's own rule for
#: project and group paths).
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")

#: A group, subgroups and the project: GitLab nests up to 20 groups.
_MIN_SEGMENTS = 2
_MAX_SEGMENTS = 21

#: Characters no repository URL the panel accepts may carry anywhere:
#: an escape (``%``) or a backslash would let two parsers read two
#: different hosts or paths, and blanks or NUL end an argument early.
_FORBIDDEN_RE = re.compile(r"[%\\\s\x00]")

#: A host name in ASCII (an IDN only in its punycode form).
_HOST_RE = re.compile(
    r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)

#: A segment of the optional path prefix of a base URL
#: (``https://git.acme.tld/gitlab``).
_PREFIX_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")

#: Hosts that are GitHub's: a GitHub repository is never a provider's.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "api.github.com"})

#: ``git@host:path`` (scp-like), the form GitLab's "Clone with SSH" shows.
_SCP_RE = re.compile(r"git@([^/:@]+):(.+)")

#: What a credential's user name and token may hold: neither needs
#: escaping in a URL, so the URL that carries them is the one rebuilt
#: here and nothing else.
_USERNAME_RE = re.compile(r"[A-Za-z0-9_.+-]{1,255}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{8,255}")


def _split_authority(authority):
    """Return ``(host, port)`` of *authority*, port None when absent.

    :param str authority: ``host`` or ``host:port``
    :rtype: tuple
    """
    host, sep, port = authority.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return authority, None


def canonical_base_url(value):
    """Return the canonical form of a provider's base URL.

    https only; the host in lower case and ASCII; the port written only
    when it is not 443; an optional path prefix whose segments are
    ``[A-Za-z0-9._-]`` and neither ``.`` nor ``..``; no user, query,
    fragment or trailing slash. GitHub's own hosts are refused: GitHub
    is configured under Settings → GitHub.

    :param str value: the base URL as typed
    :return: the canonical base URL
    :rtype: str
    :raises ValueError: with a reason fit to show
    """
    text = (value or "").strip()
    if not text.isascii():
        raise ValueError("the URL must be ASCII (write an IDN in punycode)")
    if _FORBIDDEN_RE.search(text):
        raise ValueError("the URL must not contain %, \\ or blanks")
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("the URL must start with https://")
    if "@" in parts.netloc:
        raise ValueError("the URL must not carry a user or a token")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ValueError("the URL must not carry a query or a fragment")
    host = (parts.hostname or "").lower()
    if not _HOST_RE.fullmatch(host):
        raise ValueError("the URL has no valid host name")
    if host in _GITHUB_HOSTS:
        raise ValueError("GitHub is configured under Settings → GitHub")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("the URL has an invalid port") from exc
    authority = host if port in (None, 443) else f"{host}:{port}"
    segments = [s for s in parts.path.split("/") if s]
    for segment in segments:
        if segment in (".", "..") or not _PREFIX_SEGMENT_RE.fullmatch(segment):
            raise ValueError("the URL's path is not a valid prefix")
    prefix = "".join(f"/{s}" for s in segments)
    return f"https://{authority}{prefix}"


def base_authority(base):
    """Return the ``host[:port]`` of a canonical base URL.

    :param str base: a value of :func:`canonical_base_url`
    :rtype: str
    """
    return base.split("://", 1)[1].split("/", 1)[0]


def _base_prefix(base):
    """Return the path prefix of a canonical base URL (``''`` or ``/x``).

    :param str base: a value of :func:`canonical_base_url`
    :rtype: str
    """
    rest = base.split("://", 1)[1]
    _authority, sep, prefix = rest.partition("/")
    return f"/{prefix}" if sep else ""


def _clean_path(path):
    """Return the project path of *path* as spelled, or None.

    Strips one leading and one trailing slash and a final ``.git``,
    then requires 2 to 21 segments by GitLab's rule, none ``..``-laden
    or ending in ``.atom``.

    :param str path: what follows the base's prefix
    :rtype: str or None
    """
    segments = path.strip("/").removesuffix(".git").split("/")
    if not _MIN_SEGMENTS <= len(segments) <= _MAX_SEGMENTS:
        return None
    for segment in segments:
        if (not _SEGMENT_RE.fullmatch(segment) or ".." in segment
                or segment.endswith((".atom", ".git"))):
            return None
    return "/".join(segments)


def _candidates(text):
    """Return ``(authority, host, path, ssh)`` of a repository URL, or None.

    :param str text: a stripped URL with nothing forbidden in it
    :rtype: tuple or None
    """
    scp = _SCP_RE.fullmatch(text)
    if scp:
        return None, scp.group(1).lower(), scp.group(2), True
    lowered = text.lower()
    if lowered.startswith("ssh://"):
        parts = urlsplit(text)
        if parts.username != "git" or parts.password is not None:
            return None
        return None, (parts.hostname or "").lower(), parts.path, True
    if not lowered.startswith(("https://", "http://")):
        if "://" in text:
            return None
        text = f"https://{text}"
    parts = urlsplit(text)
    if "@" in parts.netloc:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    default = 443 if parts.scheme.lower() == "https" else 80
    authority = host if port in (None, default, 443) else f"{host}:{port}"
    return authority, host, parts.path, False


def parse_provider_repo_url(value, bases):
    """Return the :data:`RepoRef` *value* names among *bases*, or None.

    The authority must be exactly a base's (``gitlab.com.evil.tld`` and
    ``evil.tld/gitlab.com/x`` match nothing), the path must start with
    that base's prefix, and the rest must be a project path. An SSH
    spelling matches on the host alone: its port is SSH's, not the
    web's. User info (other than SSH's ``git@``), a query, a fragment,
    ``%``, ``\\``, blanks and NUL make the value match nothing.

    :param str value: the URL as typed or stored
    :param bases: canonical base URLs of the configured providers
    :rtype: RepoRef or None
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not text.isascii() or _FORBIDDEN_RE.search(text):
        return None
    if "?" in text or "#" in text:
        return None
    found = _candidates(text)
    if not found:
        return None
    authority, host, path, ssh = found
    if ssh and not path.startswith("/"):
        path = f"/{path}"
    for base in bases or ():
        base_auth = base_authority(base)
        if ssh:
            if _split_authority(base_auth)[0] != host:
                continue
        elif base_auth != authority:
            continue
        prefix = _base_prefix(base)
        if prefix:
            if not path.lower().startswith(f"{prefix.lower()}/"):
                continue
            rest = path[len(prefix):]
        else:
            rest = path
        spelled = _clean_path(rest)
        if not spelled:
            continue
        return RepoRef(
            "gitlab", base, base_auth, spelled.lower(),
            f"{base}/{spelled}.git",
        )
    return None


def clone_url_with_credential(ref, credential):
    """Return the URL the build clones *ref* from, with its credential.

    ``https://<user>:<token>@<authority><prefix>/<path>.git``, rebuilt
    from the base: the ``+`` of a deploy token's user
    (``gitlab+deploy-token-N``) stays as it is, which git accepts.

    :param RepoRef ref: the repository
    :param credential: ``(username, token)``, or None for a public one
    :return: the URL, ending in ``.git``
    :rtype: str
    :raises ValueError: when the user name or the token holds a
        character that would need escaping
    """
    if not credential:
        return ref.canonical
    username, token = credential
    if not _USERNAME_RE.fullmatch(username or ""):
        raise ValueError("invalid credential user name")
    if not _TOKEN_RE.fullmatch(token or ""):
        raise ValueError("invalid credential token")
    scheme, rest = ref.canonical.split("://", 1)
    return f"{scheme}://{username}:{token}@{rest}"


def normalize_namespace(value):
    """Return a credential's namespace in lower case without edge slashes.

    :param str value: ``''``, ``group``, ``group/sub`` or a project path
    :rtype: str
    :raises ValueError: when a segment is not one GitLab allows
    """
    text = (value or "").strip().strip("/").lower()
    if not text:
        return ""
    segments = text.split("/")
    if len(segments) > _MAX_SEGMENTS or any(
        not _SEGMENT_RE.fullmatch(s) or ".." in s for s in segments
    ):
        raise ValueError("invalid namespace")
    return text


def namespace_covers(namespace, path):
    """Return whether *namespace* covers the project *path*, by segments.

    ``''`` covers every path; ``acme`` covers ``acme/x`` and
    ``acme/sub/x`` but not ``acme-other/x``.

    :param str namespace: a value of :func:`normalize_namespace`
    :param str path: a :data:`RepoRef` path (lower case)
    :rtype: bool
    """
    if not namespace:
        return True
    wanted = namespace.split("/")
    return path.lower().split("/")[:len(wanted)] == wanted
