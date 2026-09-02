"""Read GitHub's published source ranges for webhook deliveries.

``https://api.github.com/meta`` publishes the address blocks GitHub
sends webhooks from under the ``hooks`` key. An edge allowlist built
from that list is the only thing that removes the cost of verifying a
forged HMAC signature instead of merely bounding it: the signature has
to be computed over the whole body before it can be rejected, so inside
the endpoint the work is irreducible.

The list changes over time. Every consumer of this module must treat a
failed refresh as "keep the previous list" and make the staleness
visible, because a missing range silently stops webhook deliveries.
"""
import ipaddress
import json
import urllib.error
import urllib.request

from .http_utils import read_json_limited, safe_urlopen

#: The published endpoint. Fixed host, never user-supplied.
META_URL = "https://api.github.com/meta"

#: Generous for a document that is a few kB of JSON, small enough that a
#: compromised or misbehaving endpoint cannot stream us out of memory.
MAX_META_BYTES = 1 << 20

#: A plausible answer has a handful of entries. A list far beyond that
#: is a malformed document, not a legitimate expansion.
MAX_HOOK_RANGES = 64

_TIMEOUT_SECONDS = 10


class GitHubMetaError(Exception):
    """Raised when the published ranges cannot be read or trusted."""


def normalize_hook_ranges(payload):
    """Validate the ``hooks`` key of a ``/meta`` document.

    Each entry must parse as a CIDR network; anything else means the
    document is not what we think it is, and building an allowlist from
    a half-understood document would silently drop deliveries.

    :param dict payload: decoded ``/meta`` response
    :return: normalised CIDR strings, in the order GitHub published them
    :rtype: list
    :raise GitHubMetaError: when the key is missing, empty, oversized or
        contains an entry that is not a network
    """
    if not isinstance(payload, dict):
        raise GitHubMetaError("GitHub /meta did not return an object.")
    hooks = payload.get("hooks")
    if not isinstance(hooks, list) or not hooks:
        raise GitHubMetaError("GitHub /meta carries no 'hooks' ranges.")
    if len(hooks) > MAX_HOOK_RANGES:
        raise GitHubMetaError(
            f"GitHub /meta listed {len(hooks)} hook ranges, "
            f"more than the {MAX_HOOK_RANGES} we accept."
        )
    ranges = []
    for entry in hooks:
        if not isinstance(entry, str):
            raise GitHubMetaError("A hook range is not a string.")
        try:
            network = ipaddress.ip_network(entry.strip(), strict=False)
        except ValueError as exc:
            raise GitHubMetaError(
                f"Hook range {entry!r} is not a network."
            ) from exc
        ranges.append(str(network))
    return ranges


def fetch_hook_ranges(url=META_URL, timeout=_TIMEOUT_SECONDS):
    """Return the CIDR ranges GitHub delivers webhooks from.

    :param str url: endpoint override used by tests
    :param int timeout: per-request timeout in seconds
    :return: normalised CIDR strings
    :rtype: list
    :raise GitHubMetaError: on transport, decoding or validation failure
    """
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "incubacloud-webhook-allowlist",
        },
    )
    try:
        with safe_urlopen(request, timeout=timeout) as response:
            payload = read_json_limited(response, max_bytes=MAX_META_BYTES)
    except (urllib.error.URLError, OSError, ValueError,
            json.JSONDecodeError) as exc:
        raise GitHubMetaError(f"Could not read {url}: {exc}") from exc
    return normalize_hook_ranges(payload)
