"""The canonical form of the panel's MCP URL (RFC 8707, as MCP uses it).

Agents' tokens carry it as their audience, so the panel and the master
must spell it the same way: lower-case scheme and host, no default port,
no trailing slash, nothing after the path.
"""

from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"https": 443, "http": 80}


def canonical_resource(url):
    """Return *url* in canonical form.

    :param str url: an absolute http(s) URL
    :return: the canonical URL
    :raises ValueError: when *url* is not an absolute http(s) URL with an
        ASCII host, or carries credentials, a query or a fragment (even
        an empty one)
    """
    raw = (url or "").strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"not an absolute http(s) URL: {url!r}")
    # The characters themselves, not urlsplit's parts: an empty ``@``,
    # ``?`` or ``#`` leaves the parts empty and would slip through.
    if any(char in raw for char in "@?#"):
        raise ValueError(
            f"a resource URL has no credentials, query or fragment: {url!r}"
        )
    # A host in Unicode has an ASCII (punycode) spelling: two spellings
    # of one audience would not compare equal.
    if not parts.hostname.isascii():
        raise ValueError(f"the host of a resource URL is ASCII: {url!r}")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def origin_of(url):
    """Return ``scheme://host[:port]`` of *url*, canonical.

    :raises ValueError: as :func:`canonical_resource`
    """
    parts = urlsplit(canonical_resource(url))
    return f"{parts.scheme}://{parts.netloc}"


def same_origin(origin, resource):
    """Whether the browser ``Origin`` header *origin* is the panel's.

    :param str origin: the header's value (``null`` and garbage are not)
    :param str resource: the panel's canonical MCP URL
    :rtype: bool
    """
    try:
        return origin_of(origin) == origin_of(resource)
    except ValueError:
        return False


def resource_metadata_url(resource):
    """Return the RFC 9728 document of *resource*, path inserted.

    ``https://panel/mcp`` → ``https://panel/.well-known/oauth-protected-resource/mcp``,
    the URL clients try first and the one the 401 challenge names.
    """
    parts = urlsplit(canonical_resource(resource))
    return f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{parts.path}"
