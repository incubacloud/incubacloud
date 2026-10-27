"""Where an issuer publishes its signing keys.

The ``jwks_uri`` is read from the issuer's own metadata document — RFC
8414's ``/.well-known/oauth-authorization-server`` first, OpenID
Connect's ``/.well-known/openid-configuration`` when that is missing —
and only from a document whose ``issuer`` is the configured one, with
or without a trailing slash (Auth0's ends in one, the setting does not).
Cached an hour per process: issuers do not move their keys' address
often, and an agent's call must not cost a round trip to them. A failure
is remembered ``_RETRY_SECONDS``, so an issuer that is down is not asked
on every call, and the address known from before stays in use.
"""

import logging
import threading
import time
from urllib.parse import urlsplit

import requests

from ._fetch_json import fetch_json

_logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_MAX_BYTES = 64 * 1024
# How long a failed attempt is answered from memory, without asking.
_RETRY_SECONDS = 30

# {issuer: (fetched_at, jwks_uri)}
_CACHE = {}
# {issuer: (failed_at, reason)}
_FAILURES = {}
_LOCK = threading.Lock()


def issuer_spellings(issuer):
    """Return the ``iss`` values that name *issuer*: without and with a slash.

    :param str issuer: the issuer URL, as configured
    :rtype: tuple[str, str]
    """
    bare = issuer.rstrip("/")
    return bare, f"{bare}/"


def metadata_urls(issuer):
    """Return the metadata documents of *issuer*, in the order tried.

    :param str issuer: the issuer URL
    :rtype: list[str]
    """
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")
    return [
        f"{parts.scheme}://{parts.netloc}/.well-known/oauth-authorization-server{path}",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
    ]


def _discover(issuer):
    """Return the ``jwks_uri`` the metadata of *issuer* names, uncached.

    :raises requests.RequestException: when no document could be fetched
    :raises ValueError: when a document names another issuer, has no https
        ``jwks_uri``, is too big, too slow or not JSON, or none exists
    """
    last_error = None
    for url in metadata_urls(issuer):
        try:
            status, document = fetch_json(url, _MAX_BYTES)
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            continue
        if status != 200 or not isinstance(document, dict):
            continue
        if document.get("issuer") not in issuer_spellings(issuer):
            raise ValueError(
                f"the metadata at {url} names issuer "
                f"{document.get('issuer')!r}, not {issuer!r}"
            )
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
            raise ValueError(f"the metadata at {url} has no https jwks_uri")
        return jwks_uri
    if last_error is not None:
        raise last_error
    raise ValueError(f"{issuer} publishes no metadata document")


def jwks_uri_for(issuer):
    """Return the ``jwks_uri`` *issuer* publishes.

    After a failure the issuer is not asked again for ``_RETRY_SECONDS``;
    meanwhile, and when asking fails, the address known from before (even
    past its hour) answers, if there is one.

    :param str issuer: the configured issuer
    :rtype: str
    :raises requests.RequestException: when no document could be fetched
        and no address is known
    :raises ValueError: when a document names another issuer, has no https
        ``jwks_uri``, or none exists, and no address is known; or when the
        last attempt failed less than ``_RETRY_SECONDS`` ago
    """
    entry = _CACHE.get(issuer)
    if entry is not None and time.time() - entry[0] < _TTL_SECONDS:
        return entry[1]
    with _LOCK:
        now = time.time()
        entry = _CACHE.get(issuer)
        if entry is not None and now - entry[0] < _TTL_SECONDS:
            return entry[1]
        failure = _FAILURES.get(issuer)
        if failure is not None and now - failure[0] < _RETRY_SECONDS:
            if entry is not None:
                return entry[1]
            raise ValueError(
                f"the metadata of {issuer} could not be read "
                f"{now - failure[0]:.0f} s ago: {failure[1]}"
            )
        try:
            jwks_uri = _discover(issuer)
        except (requests.RequestException, ValueError) as exc:
            _FAILURES[issuer] = (time.time(), str(exc))
            if entry is None:
                raise
            _logger.warning(
                "[mcp] the metadata of %s could not be read (%s): the known "
                "jwks_uri stays in use",
                issuer,
                exc,
            )
            return entry[1]
        _FAILURES.pop(issuer, None)
        _CACHE[issuer] = (time.time(), jwks_uri)
        _logger.info("[mcp] issuer %s publishes its keys at %s", issuer, jwks_uri)
        return jwks_uri


def invalidate(issuer=None):
    """Drop cached documents. Test helper and manual escape hatch.

    :param issuer: drop only this one; None clears everything
    """
    with _LOCK:
        if issuer is None:
            _CACHE.clear()
            _FAILURES.clear()
        else:
            _CACHE.pop(issuer, None)
            _FAILURES.pop(issuer, None)
