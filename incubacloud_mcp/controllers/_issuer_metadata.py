"""Where an issuer publishes its signing keys.

The ``jwks_uri`` is read from the issuer's own metadata document — RFC
8414's ``/.well-known/oauth-authorization-server`` first, OpenID
Connect's ``/.well-known/openid-configuration`` when that is missing —
and only from a document whose ``issuer`` is the configured one. Cached
an hour per process: issuers do not move their keys' address often, and
an agent's call must not cost a round trip to them.
"""

import logging
import threading
import time
from urllib.parse import urlsplit

import requests

_logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_MAX_BYTES = 64 * 1024
_HTTP_TIMEOUT = 5

# {issuer: (fetched_at, jwks_uri)}
_CACHE = {}
_LOCK = threading.Lock()


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


def _fetch_document(url):
    """Return the JSON document at *url*, or None when it is not there.

    :raises requests.RequestException: when the issuer cannot be reached
    :raises ValueError: when the document is too big or not JSON
    """
    response = requests.get(url, timeout=_HTTP_TIMEOUT, allow_redirects=False)
    if response.status_code != 200:
        return None
    if len(response.content) > _MAX_BYTES:
        raise ValueError(f"metadata document at {url} exceeds {_MAX_BYTES} bytes")
    data = response.json()
    return data if isinstance(data, dict) else None


def jwks_uri_for(issuer):
    """Return the ``jwks_uri`` *issuer* publishes.

    :param str issuer: the configured issuer
    :rtype: str
    :raises requests.RequestException: when no document could be fetched
    :raises ValueError: when a document names another issuer, has no https
        ``jwks_uri``, or none exists
    """
    entry = _CACHE.get(issuer)
    if entry is not None and time.time() - entry[0] < _TTL_SECONDS:
        return entry[1]
    with _LOCK:
        entry = _CACHE.get(issuer)
        if entry is not None and time.time() - entry[0] < _TTL_SECONDS:
            return entry[1]
        last_error = None
        for url in metadata_urls(issuer):
            try:
                document = _fetch_document(url)
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                continue
            if document is None:
                continue
            if document.get("issuer") != issuer:
                raise ValueError(
                    f"the metadata at {url} names issuer "
                    f"{document.get('issuer')!r}, not {issuer!r}"
                )
            jwks_uri = document.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
                raise ValueError(f"the metadata at {url} has no https jwks_uri")
            _CACHE[issuer] = (time.time(), jwks_uri)
            _logger.info("[mcp] issuer %s publishes its keys at %s", issuer, jwks_uri)
            return jwks_uri
        if last_error is not None:
            raise last_error
        raise ValueError(f"{issuer} publishes no metadata document")


def invalidate(issuer=None):
    """Drop cached documents. Test helper and manual escape hatch.

    :param issuer: drop only this one; None clears everything
    """
    with _LOCK:
        if issuer is None:
            _CACHE.clear()
        else:
            _CACHE.pop(issuer, None)
