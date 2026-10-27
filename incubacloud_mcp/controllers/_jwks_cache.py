"""Process-local cache for the IdP's JWKS.

Why this exists (SEC-011):
    Both OIDC entry points downloaded the key set on every single call,
    *before* deciding whether the token was worth verifying. On
    ``/oauth/backchannel_logout`` — public, POST, CSRF-exempt — that
    turned three bytes of garbage into an outbound HTTPS request to the
    manager and up to ``timeout`` seconds of an occupied worker. Cheap
    for the sender, expensive for us, and multiplied across the fleet it
    aims the whole estate at the manager's ``/oauth/jwks``.

    With the key set cached, a garbage POST costs no network at all.

Freshness:
    Signing keys rotate, so a cache that only expired on a timer would
    reject good tokens for up to the TTL after a rotation. Instead the
    caller passes the ``kid`` it needs: a key set that does not contain
    it is refetched immediately, whatever the TTL says. The TTL then
    only covers the case nobody asks about — a key silently removed
    upstream.

Minimum wait:
    An unknown ``kid`` is the attacker's cheapest lever: a token
    with a random one used to cost a download each. The set is now
    fetched again for an unknown ``kid`` at most once every
    ``_MIN_REFRESH_SECONDS`` per process. A real rotation still lands
    at once — the previous fetch is long past by then.

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
from joserfc.jwk import KeySet

_logger = logging.getLogger(__name__)

# How long a key set is reused when nobody reports an unknown ``kid``.
_TTL_SECONDS = 600
# Cap on the JWKS document itself. A legitimate key set is a couple of
# kB; this stops a compromised or misconfigured endpoint from feeding us
# an unbounded body.
_MAX_JWKS_BYTES = 128 * 1024
_HTTP_TIMEOUT = 5
# At most one refetch for an unknown ``kid`` in this many seconds.
_MIN_REFRESH_SECONDS = 30

# {jwks_uri: (fetched_at, KeySet, frozenset_of_kids)}
_CACHE = {}
_LOCK = threading.Lock()


def unverified_kid(token):
    """Return the ``kid`` from a compact JWT header, or None.

    Read *before* any signature check and therefore trusted for exactly
    one thing: choosing whether the cached key set can answer. A token
    naming an unknown ``kid`` buys one refetch — bounded by the rate
    limit — and still has to pass the signature check afterwards.

    The joserfc 1.6 series exposes no header-only decode, so the first
    segment is parsed here rather than through a ``getattr`` probe that
    would silently degrade to "no kid" and make key rotation invisible
    until the cache TTL expired.

    :param token: the compact JWT.
    :return: the ``kid`` string, or None when absent or unparseable.
    """
    try:
        segment = token.split(".", 1)[0]
        padded = segment + "=" * (-len(segment) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(header, dict):
        return None
    kid = header.get("kid")
    return kid if isinstance(kid, str) else None


def _fetch(jwks_uri):
    """Download and parse the key set at *jwks_uri*.

    :param jwks_uri: absolute URL of the IdP's JWKS document.
    :return: tuple of the parsed ``KeySet`` and the set of its ``kid``s.
    :raises ValueError: if the document is larger than the cap.
    """
    r = requests.get(jwks_uri, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    if len(r.content) > _MAX_JWKS_BYTES:
        raise ValueError(f"JWKS document at {jwks_uri} exceeds {_MAX_JWKS_BYTES} bytes")
    data = r.json()
    keyset = KeySet.import_key_set(data)
    kids = frozenset(k.get("kid") for k in (data.get("keys") or []) if k.get("kid"))
    return keyset, kids


def _usable(entry, kid, now):
    """Whether a cached *entry* answers for *kid* at *now*.

    Fresh and holding *kid*: yes. Fresh, without *kid*, fetched less
    than ``_MIN_REFRESH_SECONDS`` ago: yes too — the caller fails on
    the missing key instead of buying a download. Expired: no.

    :param entry: ``(fetched_at, keyset, kids)``
    :param kid: the ``kid`` asked for, or None
    :param float now: the current time
    :rtype: bool
    """
    fetched_at, _keyset, kids = entry
    age = now - fetched_at
    if age >= _TTL_SECONDS:
        return False
    if kid is None or kid in kids:
        return True
    return age < _MIN_REFRESH_SECONDS


def get_keyset(jwks_uri, kid=None):
    """Return the key set for *jwks_uri*, fetching only when needed.

    Refetches when the entry is missing, older than the TTL, or does not
    carry *kid* — the last case is what makes a key rotation visible
    immediately instead of at the next expiry. Refetches for an unknown
    ``kid`` at most once every ``_MIN_REFRESH_SECONDS``.

    :param jwks_uri: absolute URL of the IdP's JWKS document.
    :param kid: key id the caller needs, read from the token header. A
        cached set without it is considered stale. ``None`` skips that
        check.
    :return: a ``joserfc.jwk.KeySet``.
    """
    entry = _CACHE.get(jwks_uri)
    if entry is not None and _usable(entry, kid, time.time()):
        return entry[1]

    with _LOCK:
        # Re-read under the lock: another thread may have refreshed
        # while we waited, and a rotation should cost one fetch, not one
        # per concurrent request.
        entry = _CACHE.get(jwks_uri)
        if entry is not None and _usable(entry, kid, time.time()):
            return entry[1]
        keyset, kids = _fetch(jwks_uri)
        _CACHE[jwks_uri] = (time.time(), keyset, kids)
        _logger.info(
            "[oidc] fetched JWKS from %s (%d key(s))",
            jwks_uri,
            len(kids),
        )
        return keyset


def invalidate(jwks_uri=None):
    """Drop cached key sets. Test helper and manual escape hatch.

    :param jwks_uri: drop only this entry; ``None`` clears everything.
    """
    with _LOCK:
        if jwks_uri is None:
            _CACHE.clear()
        else:
            _CACHE.pop(jwks_uri, None)
