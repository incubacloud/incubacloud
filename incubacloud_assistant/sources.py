"""The panel's public sources: its user documentation and core's code.

Only inside core's repository — the folder that holds the ``incubacloud``
module — and only its public entries, a closed list. Never anything else
on the disk: on the master and on the tenant panels, saas's private code
is installed beside it. A path with ``..``, a hidden part, a link that
leaves the repository or a binary file is refused; a vendored library and
a generated site are not searched. Where the image carries no ``docs``
folder, the documentation is searched on its published site instead.
"""

import html
import os
import re
import time
import unicodedata

import requests

from odoo.modules.module import get_module_path

#: Top-level entries of core's repository the assistant reads.
PUBLIC_ENTRIES = frozenset(
    {
        "auth_oauth_github",
        "incubacloud",
        "incubacloud_assistant",
        "incubacloud_connect",
        "incubacloud_mcp",
        "incubacloud_mcp_pitr",
        "incubacloud_pitr",
        "docs",
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "LICENSE",
        "requirements.txt",
        "oca_dependencies.txt",
    }
)
#: Tracked entries left out on purpose: the repository's tooling and notes.
NOT_READ = frozenset(
    {
        ".github",
        ".gitignore",
        ".gitleaks.toml",
        ".impeccable.md",
        ".pre-commit-config.yaml",
        "AGENTS.md",
        "CLA.md",
        "CODE_OF_CONDUCT.md",
        "requirements-ci.txt",
    }
)
#: Folders never searched: generated or caches. Vendored libraries are the
#: ``lib`` folders of ``static`` only (``incubacloud/scripts/lib`` is ours).
_SKIPPED_DIRS = frozenset({"site", "node_modules", "__pycache__"})
#: Kinds of file read and searched.
TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".xml",
        ".js",
        ".scss",
        ".css",
        ".md",
        ".txt",
        ".csv",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".sh",
        ".bats",
        ".sql",
        ".cfg",
    }
)
#: Largest file read.
MAX_FILE_BYTES = 256 * 1024
#: Seconds one search may take.
SEARCH_SECONDS = 5
#: The user documentation, and where it is published.
DOCS_DIR = ("docs", "user", "docs")
DOCS_URL = "https://docs.incubacloud.io/"
#: The search index mkdocs publishes with the site, read when an image
#: carries no ``docs`` folder: Doodba's ``400-clean`` keeps only the
#: modules of core's repository, on the master and on the tenant panels.
PUBLISHED_INDEX_URL = DOCS_URL + "search/search_index.json"
#: Seconds the published index is kept before it is read again.
PUBLISHED_INDEX_SECONDS = 3600
#: The published sections last read, and when: ``{"at", "sections"}``.
_published = {"at": 0.0, "sections": None}
_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_TAG_RE = re.compile(r"<[^>]+>")


class SourceError(ValueError):
    """A path the assistant may not read."""


def core_root():
    """Return core's repository folder, or None when it cannot be told.

    The folder above the ``incubacloud`` module's real path (Doodba links
    the module into ``auto/addons``), which must hold its manifest.
    """
    path = get_module_path("incubacloud")
    if not path:
        return None
    module = os.path.realpath(path)
    root = os.path.dirname(module)
    if os.path.basename(module) != "incubacloud" or not os.path.isfile(
        os.path.join(module, "__manifest__.py")
    ):
        return None
    return root


def _relative(root, path):
    """Return the real path of *path* under *root*, checked; raise otherwise."""
    text = str(path or "").strip().replace("\\", "/").strip("/")
    if "\x00" in text:
        raise SourceError("A path holds no NUL character.")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise SourceError("A path never climbs with '..'.")
    if not parts:
        return root, ""
    real = os.path.realpath(os.path.join(root, *parts))
    relative = os.path.relpath(real, root)
    pieces = relative.split(os.sep)
    if (
        relative.startswith("..")
        or os.path.isabs(relative)
        or pieces[0] not in PUBLIC_ENTRIES
        or any(piece.startswith(".") for piece in pieces)
        or relative != os.path.join(*parts)
    ):
        raise SourceError(f"{text} is not in the panel's public code.")
    return real, relative


def _is_text(path):
    """Return whether *path* is a text file the assistant reads."""
    if os.path.splitext(path)[1].lower() not in TEXT_EXTENSIONS and (
        os.path.basename(path) != "LICENSE"
    ):
        return False
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return False
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    return b"\x00" not in head


def _files(root, start):
    """Yield ``(real path, relative path)`` of the text files under *start*."""
    for folder, dirs, files in os.walk(start, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".")
            and name not in _SKIPPED_DIRS
            and not (name == "lib" and os.path.basename(folder) == "static")
            and not os.path.islink(os.path.join(folder, name))
        )
        if folder == root:
            dirs[:] = [name for name in dirs if name in PUBLIC_ENTRIES]
        for name in sorted(files):
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root)
            if (
                name.startswith(".")
                or os.path.islink(path)
                or relative.split(os.sep)[0] not in PUBLIC_ENTRIES
                or not _is_text(path)
            ):
                continue
            yield path, relative


def search_code(root, text, path, limit):
    """Find *text* (case-insensitive) in the public code; at most *limit* lines.

    :return: ``{matches: [{path, line, text}], truncated}``
    :raises SourceError: for a *path* out of the public code
    """
    needle = (text or "").lower()
    if len(needle) < 2:
        raise SourceError("Search for two characters at least.")
    start, _relative_path = _relative(root, path) if path else (root, "")
    deadline = time.monotonic() + SEARCH_SECONDS
    matches, truncated = [], False
    for real, relative in _files(root, start):
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            with open(real, encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    if needle in line.lower():
                        matches.append(
                            {
                                "path": relative,
                                "line": number,
                                "text": line.strip()[:300],
                            }
                        )
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
        except (OSError, UnicodeDecodeError):
            continue
    return {"matches": matches, "truncated": truncated}


def read_source(root, path, start_line, lines):
    """Read *lines* lines of a public file from *start_line*, or list a folder.

    :return: ``{path, lines: ["n: text"], total, next_line}`` for a file,
        ``{path, entries}`` for a folder (folders end with ``/``)
    :raises SourceError: for a path out of the public code, or a file that
        is not text
    """
    real, relative = _relative(root, path)
    if os.path.isdir(real):
        names = []
        for name in sorted(os.listdir(real)):
            full = os.path.join(real, name)
            if name.startswith(".") or os.path.islink(full):
                continue
            if real == root and name not in PUBLIC_ENTRIES:
                continue
            names.append(f"{name}/" if os.path.isdir(full) else name)
        return {"path": relative, "entries": names}
    if not os.path.isfile(real) or not _is_text(real):
        raise SourceError(f"{relative} is not a text file of the panel's code.")
    with open(real, encoding="utf-8", errors="replace") as handle:
        all_lines = handle.read().splitlines()
    first = max(1, int(start_line or 1))
    count = max(1, min(int(lines or 400), 2000))
    chunk = all_lines[first - 1 : first - 1 + count]
    following = first + len(chunk)
    return {
        "path": relative,
        "lines": [f"{first + index}: {text}" for index, text in enumerate(chunk)],
        "total": len(all_lines),
        "next_line": following if following <= len(all_lines) else None,
    }


def _words(text):
    """Return the words of *text*, lower case and without accents."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return _WORD_RE.findall(plain)


def _slug(heading):
    """Return the anchor mkdocs gives *heading*."""
    words = re.sub(r"[^\w\s-]", "", heading.lower()).split()
    return "-".join(words)


def _page_url(relative):
    """Return the published page of a file of the user documentation."""
    page = relative[: -len(".md")].removesuffix("index")
    return DOCS_URL + (f"{page}/" if page and not page.endswith("/") else page)


def _sections(docs):
    """Yield ``(page, heading, text, url)`` of every section of the docs folder."""
    for relative, heading, text in _file_sections(docs):
        url = _page_url(relative) + (f"#{_slug(heading)}" if heading else "")
        yield relative, heading, text, url


def _file_sections(docs):
    """Yield ``(relative file, heading, text)`` of every section under *docs*."""
    for folder, dirs, files in os.walk(docs, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = os.path.join(folder, name)
            if os.path.islink(path):
                continue
            relative = os.path.relpath(path, docs).replace(os.sep, "/")
            heading, lines = "", []
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    match = _HEADING_RE.match(line.rstrip())
                    if match:
                        if lines or heading:
                            yield relative, heading, "".join(lines).strip()
                        heading, lines = match.group(2).strip(), []
                    else:
                        lines.append(line)
            yield relative, heading, "".join(lines).strip()


def search_docs(root, query, limit):
    """Return the sections of the user documentation that match *query* best.

    Keywords count three times in a heading; each section answers its
    heading, the first 800 characters and its page on the docs site.

    :return: ``{sections: [{page, heading, url, text}]}``
    """
    terms = set(_words(query or ""))
    if not terms:
        raise SourceError("Give keywords, in English.")
    docs = os.path.join(root, *DOCS_DIR)
    found = _sections(docs) if os.path.isdir(docs) else _published_sections()
    scored = []
    for page, heading, text, url in found:
        heading_words, text_words = _words(heading), _words(text)
        score = sum(3 * heading_words.count(t) + text_words.count(t) for t in terms)
        if score:
            scored.append((score, page, heading, text, url))
    scored.sort(key=lambda item: (-item[0], item[1], item[4]))
    return {
        "sections": [
            {"page": page, "heading": heading, "url": url, "text": text[:800]}
            for _score, page, heading, text, url in scored[:limit]
        ]
    }


def _published_sections():
    """Return ``[(page, heading, text, url)]`` of the published documentation.

    Read from the site's search index and kept for
    ``PUBLISHED_INDEX_SECONDS``; when the site cannot be read, the copy
    last read serves, however old.

    :raise SourceError: the site cannot be read and nothing was read before.
    """
    now = time.monotonic()
    cached = _published["sections"]
    if cached is not None and now - _published["at"] < PUBLISHED_INDEX_SECONDS:
        return cached
    try:
        response = requests.get(PUBLISHED_INDEX_URL, timeout=SEARCH_SECONDS)
        response.raise_for_status()
        entries = response.json()["docs"]
        sections = [
            (
                entry["location"].partition("#")[0],
                entry["title"],
                " ".join(html.unescape(_TAG_RE.sub(" ", entry["text"])).split()),
                DOCS_URL + entry["location"],
            )
            for entry in entries
        ]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        if cached is not None:
            return cached
        raise SourceError(
            f"The documentation cannot be searched right now; it is at {DOCS_URL}."
        ) from None
    _published.update(at=now, sections=sections)
    return sections
