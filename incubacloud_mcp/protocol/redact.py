"""What leaves the panel through an agent: no secret, no live link.

The routes' contract is already ``has_<field>``, never the value. This is
the safety net behind it: keys named like an encrypted field are dropped,
and every known secret value is masked wherever it appears in text —
logs, command output, job logs, alerts, captured mail. It is a
mitigation: a developer with ``run_command`` can still read and encode a
secret of the instance, as with the terminal.
"""
import base64
import re
from urllib.parse import quote

MASK = "[secreto]"
#: Shorter values are not masked: they would shred the output.
MIN_SECRET_LEN = 8

#: Links that carry a one-time credential in captured mail.
_TOKEN_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*?"
    r"(?:/web/reset_password\?|/web/signup\?|/ic/login\?t=|[?&]token=|[?&]access_token=)"
    r"[^\s\"'<>]*",
    re.IGNORECASE,
)
#: ``scheme://user:password@host``: a repository URL with its credential.
_URL_CREDENTIAL_RE = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)


def secret_variants(value):
    """Return the forms *value* may take in output.

    :rtype: set[str]
    """
    if not value or len(value) < MIN_SECRET_LEN:
        return set()
    return {value, quote(value, safe=""), base64.b64encode(value.encode()).decode("ascii")}


class Scrubber:
    """Mask known secret values, and credentials in URLs, in text."""

    def __init__(self, values):
        """Build the matcher, longest variant first.

        :param values: the decrypted secrets of the database
        """
        variants = set()
        for value in values:
            variants |= secret_variants(value)
        ordered = sorted(variants, key=len, reverse=True)
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None

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


def mask_token_urls(obj):
    """Replace, inside *obj*, every URL that carries a one-time token."""
    if isinstance(obj, str):
        return _TOKEN_URL_RE.sub(MASK, obj)
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
        return {key: drop_secret_keys(item, names) for key, item in obj.items() if key not in names}
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
