"""Process-local cache for the IdP's JWKS.

Why this exists (SEC-011):
    Both OIDC entry points downloaded the key set on every single call,
    *before* deciding whether the token was worth verifying. On
    ``/oauth/backchannel_logout`` — public, POST, CSRF-exempt — that
    turned three bytes of garbage into an outbound HTTPS request to the
    manager and an occupied worker for as long as the download took
    (``requests``' ``timeout`` bounds each read, not the whole body).
    Cheap for the sender, expensive for us, and multiplied across the
    fleet it aims the whole estate at the manager's ``/oauth/jwks``.

    With the key set cached, a garbage POST costs no network at all, and
    a download, when one is due, is bounded in size and in total time
    (``_fetch_json``).

Freshness:
    Signing keys rotate, so a cache that only expired on a timer would
    reject good tokens for up to the TTL after a rotation. Instead the
    caller passes the ``kid`` it needs: a key set that does not contain
    it is fetched again as soon as the last download is
    ``_MIN_REFRESH_SECONDS`` old. The TTL then only covers the case
    nobody asks about — a key silently removed upstream.

Minimum wait:
    An unknown ``kid`` is the attacker's cheapest lever: a token
    with a random one used to cost a download each. The set is now
    downloaded at most once every ``_MIN_REFRESH_SECONDS`` per process,
    whatever asks and whether the download works or fails. A real
    rotation still lands at once — the previous download is long past
    by then.

Issuer down:
    A failed download keeps the keys already known, expired or not: a
    token signed with one of them still verifies, and only a ``kid``
    never seen waits for the issuer to answer again.

Home:
    Moved from ``incubacloud_tenant`` to ``incubacloud_mcp``, which
    validates agents' tokens with it; the tenant's single sign-on
    and back-channel logout import it from here.

Scope:
    Per worker process, not cluster-wide, matching the existing
    ``_CATALOG_CACHE`` in the manager's ``vps_api``. A handful of
    fetches per minute across workers is a non-issue; a shared cache
    would cost operational machinery this does not need.
"""

import base64
import binascii
import json
import logging
import threading
import time

import requests
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from ._fetch_json import fetch_json

_logger = logging.getLogger(__name__)

# How long a key set is reused when nobody reports an unknown ``kid``.
_TTL_SECONDS = 600
# Cap on the JWKS document itself. A legitimate key set is a couple of
# kB; this stops a compromised or misconfigured endpoint from feeding us
# an unbounded body.
_MAX_JWKS_BYTES = 128 * 1024
# At most one download for each key set in this many seconds.
_MIN_REFRESH_SECONDS = 30
# A JWT header is a few dozen bytes; past this its ``kid`` is not read.
_MAX_HEADER_CHARS = 2048

# {jwks_uri: (fetched_at, KeySet, frozenset_of_kids)}: the last good set.
_CACHE = {}
# {jwks_uri: (attempted_at, failed)}: the last download, good or not.
_ATTEMPTS = {}
_LOCK = threading.Lock()


def unverified_kid(token):
    """Return the ``kid`` from a compact JWT header, or None.

    Read *before* any signature check and therefore trusted for exactly
    one thing: choosing whether the cached key set can answer. A token
    naming an unknown ``kid`` buys at most one download every
    ``_MIN_REFRESH_SECONDS`` and still has to pass the signature check
    afterwards.

    The joserfc 1.6 series exposes no header-only decode, so the first
    segment is parsed here rather than through a ``getattr`` probe that
    would silently degrade to "no kid" and make key rotation invisible
    until the cache TTL expired.

    :param token: the compact JWT.
    :return: the ``kid`` string, or None when absent, unparseable, longer
        than ``_MAX_HEADER_CHARS`` or nested past the parser's depth.
    """
    try:
        segment = token.split(".", 1)[0]
        if len(segment) > _MAX_HEADER_CHARS:
            return None
        padded = segment + "=" * (-len(segment) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(header, dict):
        return None
    kid = header.get("kid")
    return kid if isinstance(kid, str) else None


def _fetch(jwks_uri):
    """Download and parse the key set at *jwks_uri*.

    :param jwks_uri: absolute URL of the IdP's JWKS document.
    :return: tuple of the parsed ``KeySet`` and the set of its ``kid``s.
    :raises requests.RequestException: when it cannot be reached or does
        not answer 200.
    :raises ValueError: when the document is too big, too slow, or not a
        key set whose keys can all be read.
    """
    status, data = fetch_json(jwks_uri, _MAX_JWKS_BYTES)
    if status != 200:
        raise requests.HTTPError(f"the key set at {jwks_uri} answered HTTP {status}")
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
        raise ValueError(f"the document at {jwks_uri} is not a key set")
    try:
        keyset = KeySet.import_key_set(data)
    except (JoseError, TypeError, ValueError) as exc:
        raise ValueError(
            f"a key of the set at {jwks_uri} cannot be read: {exc}"
        ) from exc
    kids = frozenset(key["kid"] for key in keys if isinstance(key.get("kid"), str))
    return keyset, kids


def _usable(entry, kid, now):
    """Whether a cached *entry* answers for *kid* at *now* without asking.

    Fresh and holding *kid* (or no *kid* asked for): yes. Anything else
    is decided under the lock, against the last download.

    :param entry: ``(fetched_at, keyset, kids)``
    :param kid: the ``kid`` asked for, or None
    :param float now: the current time
    :rtype: bool
    """
    fetched_at, _keyset, kids = entry
    return now - fetched_at < _TTL_SECONDS and (kid is None or kid in kids)


def get_keyset(jwks_uri, kid=None):
    """Return the key set for *jwks_uri*, fetching only when needed.

    Downloads when the entry is missing, older than the TTL, or does not
    carry *kid* — the last case is what makes a key rotation visible
    without waiting for the next expiry — and the last download, good or
    failed, is at least ``_MIN_REFRESH_SECONDS`` old. Meanwhile it answers
    with the set it has: the caller fails on a missing key instead of
    buying a download. A failed download keeps the known keys in use.

    :param jwks_uri: absolute URL of the IdP's JWKS document.
    :param kid: key id the caller needs, read from the token header. A
        cached set without it is considered stale. ``None`` skips that
        check.
    :return: a ``joserfc.jwk.KeySet``.
    :raises requests.RequestException: when the download fails and no
        known key can answer
    :raises ValueError: likewise, or when the last download failed less
        than ``_MIN_REFRESH_SECONDS`` ago and no known key can answer
    """
    entry = _CACHE.get(jwks_uri)
    if entry is not None and _usable(entry, kid, time.time()):
        return entry[1]

    with _LOCK:
        # Re-read under the lock: another thread may have refreshed
        # while we waited, and a rotation should cost one fetch, not one
        # per concurrent request.
        now = time.time()
        entry = _CACHE.get(jwks_uri)
        if entry is not None and _usable(entry, kid, now):
            return entry[1]
        known = entry is not None and (kid is None or kid in entry[2])
        attempted_at, failed = _ATTEMPTS.get(jwks_uri, (0.0, False))
        if now - attempted_at < _MIN_REFRESH_SECONDS:
            if known or (entry is not None and not failed):
                return entry[1]
            raise ValueError(
                f"the key set at {jwks_uri} could not be downloaded "
                f"{now - attempted_at:.0f} s ago"
            )
        _ATTEMPTS[jwks_uri] = (now, True)
        try:
            keyset, kids = _fetch(jwks_uri)
        except (requests.RequestException, ValueError) as exc:
            if not known:
                raise
            _logger.warning(
                "[mcp] the key set at %s could not be downloaded (%s): "
                "the known keys stay in use",
                jwks_uri,
                exc,
            )
            return entry[1]
        _ATTEMPTS[jwks_uri] = (time.time(), False)
        _CACHE[jwks_uri] = (time.time(), keyset, kids)
        _logger.info("[mcp] fetched JWKS from %s (%d key(s))", jwks_uri, len(kids))
        return keyset


def invalidate(jwks_uri=None):
    """Drop cached key sets. Test helper and manual escape hatch.

    :param jwks_uri: drop only this entry; ``None`` clears everything.
    """
    with _LOCK:
        if jwks_uri is None:
            _CACHE.clear()
            _ATTEMPTS.clear()
        else:
            _CACHE.pop(jwks_uri, None)
            _ATTEMPTS.pop(jwks_uri, None)
