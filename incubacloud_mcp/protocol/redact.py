"""What leaves the panel through an agent: no secret, no live link.

The routes' contract is already ``has_<field>``, never the value. This is
the safety net behind it: keys named like an encrypted field are dropped,
and every known secret value is masked wherever it appears in text —
logs, command output, job logs, alerts, captured mail. It is a
mitigation: a developer with ``run_command`` can still read and encode a
secret of the instance, as with the terminal.
"""

import base64
import functools
import json
import re
from urllib.parse import quote

MASK = "[secreto]"
#: Shorter values are not masked: they would shred the output.
MIN_SECRET_LEN = 8

#: A link in text or HTML. Found first, then looked into: one pattern for
#: both would rescan the rest of the text from every ``http``.
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
#: What makes a link a one-time credential in captured mail. ``token=``
#: anywhere, so that HTML's ``&amp;token=`` and ``access_token=`` count.
_TOKEN_MARK_RE = re.compile(
    r"token=|/web/reset_password\?|/web/signup\?|/ic/login\?", re.IGNORECASE
)
#: ``scheme://<userinfo>@host``: a repository URL with its credential —
#: ``user:password``, a bare token or ``:token``. The scheme is bounded so
#: a long dotted word costs no rescan.
_URL_CREDENTIAL_RE = re.compile(
    r"\b([a-z][a-z0-9+.-]{0,31}://)[^/?#\s@]+@", re.IGNORECASE
)

#: Keys of a stored JSON document whose text values are secrets.
_SECRET_KEY_RE = re.compile(r"password|secret|token|passphrase|key", re.IGNORECASE)
#: Keys that never hold one, whatever they are called next to.
_PUBLIC_KEYS = frozenset({"machine", "login"})
#: A line of a PEM block's body.
_PEM_LINE_RE = re.compile(rf"[A-Za-z0-9+/=]{{{MIN_SECRET_LEN},}}")


def _json_leaves(node, secret=False):
    """Yield the text leaves of *node* that sit under a secret key."""
    if isinstance(node, dict):
        for key, item in node.items():
            name = str(key)
            yield from _json_leaves(
                item,
                name not in _PUBLIC_KEYS and bool(_SECRET_KEY_RE.search(name)),
            )
    elif isinstance(node, list):
        for item in node:
            yield from _json_leaves(item, secret)
    elif secret and isinstance(node, str):
        yield node


def secret_parts(value):
    """Return what one stored secret hides: itself and its pieces.

    A JSON object adds its text values under keys that name a secret
    (``password``, ``client_secret``, ``new_usage_token``…, never
    ``machine`` or ``login``), at any depth; a PEM block adds each line
    of its body, so a key printed a line at a time is masked too.

    :param str value: a decrypted secret
    :rtype: set[str]
    """
    parts = {value}
    if value.lstrip().startswith("{"):
        try:
            document = json.loads(value)
        except (ValueError, RecursionError):
            document = None
        for leaf in _json_leaves(document):
            parts |= secret_parts(leaf)
    if "-----BEGIN " in value:
        parts.update(
            line.strip()
            for line in value.splitlines()
            if _PEM_LINE_RE.fullmatch(line.strip())
        )
    return parts


def secret_variants(value):
    """Return the forms *value* may take in output.

    :rtype: set[str]
    """
    if not value or len(value) < MIN_SECRET_LEN:
        return set()
    return {
        value,
        quote(value, safe=""),
        base64.b64encode(value.encode()).decode("ascii"),
    }


def _trie_pattern(words):
    """Return a regex of *words* as a prefix tree: one branch per next character.

    A flat alternation tries every word at every position of the text; a
    tree tries one character at a time, so a thousand secrets cost little
    more than one. Each optional continuation is greedy: the longest word
    that matches wins, as in the flat alternation, longest first. Built
    without recursion: a word may be thousands of characters long.

    :param words: non-empty strings
    :rtype: str
    """
    root = {}
    for word in words:
        node = root
        for char in word:
            node = node.setdefault(char, {})
        node[""] = None
    patterns = {}
    stack = [(root, False)]
    while stack:
        node, children_done = stack.pop()
        if not children_done:
            stack.append((node, True))
            stack.extend((child, False) for char, child in node.items() if char)
            continue
        branches = [
            re.escape(char) + patterns.pop(id(child))
            for char, child in sorted(node.items())
            if char
        ]
        ends_here = "" in node
        if not branches:
            pattern = ""
        elif len(branches) == 1 and not ends_here:
            pattern = branches[0]
        else:
            pattern = "(?:" + "|".join(branches) + ")" + ("?" if ends_here else "")
        patterns[id(node)] = pattern
    return patterns[id(root)]


@functools.lru_cache(maxsize=16)
def _compiled(values):
    """Return the pattern of every form of *values*, longest first, or None.

    Cached by the set itself: the same secrets, call after call, are
    compiled once per process. Secrets whose tree nests too deep for the
    regex compiler fall back to a flat alternation, longest first.

    :param frozenset values: the decrypted secrets
    """
    variants = set()
    for value in values:
        variants |= secret_variants(value)
    if not variants:
        return None
    try:
        return re.compile(_trie_pattern(variants))
    except (RecursionError, re.error):
        ordered = sorted(variants, key=lambda variant: (-len(variant), variant))
        return re.compile("|".join(map(re.escape, ordered)))


class Scrubber:
    """Mask known secret values, and credentials in URLs, in text."""

    def __init__(self, values):
        """Build the matcher, longest variant first.

        :param values: the decrypted secrets of the database
        """
        self._pattern = _compiled(frozenset(values))

    def text(self, text):
        """Return *text* with every known secret and URL credential masked."""
        if not isinstance(text, str) or not text:
            return text
        if self._pattern:
            text = self._pattern.sub(MASK, text)
        return _URL_CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}{MASK}@", text)

    def value(self, obj):
        """Apply :meth:`text` to every string inside *obj*."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {key: self.value(item) for key, item in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.value(item) for item in obj]
        return obj


def _mask_token_url(match):
    """Return the mask for a link that carries a token, else the link."""
    url = match.group(0)
    return MASK if _TOKEN_MARK_RE.search(url) else url


def mask_token_urls(obj):
    """Replace, inside *obj*, every URL that carries a one-time token."""
    if isinstance(obj, str):
        return _URL_RE.sub(_mask_token_url, obj)
    if isinstance(obj, dict):
        return {key: mask_token_urls(item) for key, item in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_token_urls(item) for item in obj]
    return obj


def drop_secret_keys(obj, names):
    """Return *obj* without any key in *names*, at any depth.

    :param names: the encrypted fields' names (never ``has_*``)
    """
    if isinstance(obj, dict):
        return {
            key: drop_secret_keys(item, names)
            for key, item in obj.items()
            if key not in names
        }
    if isinstance(obj, (list, tuple)):
        return [drop_secret_keys(item, names) for item in obj]
    return obj


def find_secret_key(obj, names, path="arguments"):
    """Return the path of the first key in *names* inside *obj*, or None."""
    if isinstance(obj, dict):
        for key, item in obj.items():
            if key in names:
                return f"{path}.{key}"
            found = find_secret_key(item, names, f"{path}.{key}")
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            found = find_secret_key(item, names, f"{path}[{index}]")
            if found:
                return found
    return None
