"""Validate a URL a host is about to be told to download from.

Restoring from a link moves the transfer off the operator's connection
and onto the host, which is what makes a 20 GB archive practical. It
also turns the panel into something that fetches an address somebody
else chose, from inside the network — the shape of every SSRF. So the
same guard the outbound webhooks use applies here, with three
differences the use case forces:

* three schemes rather than one. ``sftp`` and ``ftp`` are where backups
  from another provider usually live, and ``curl`` on the host speaks
  both.
* credentials are allowed, because those two schemes normally need
  them — but they are split off the URL immediately. What travels in
  the job payload, the log and the interface is the masked form; the
  secret reaches the host as a file, never as a word in a command line
  that ``ps`` would show.
* the resolved address is pinned and handed to ``curl``, so the name
  cannot resolve to a public address here and a private one there. The
  link curl gets is rebuilt from the name checked (``curl_url``), so the
  pin always matches it; a name that is not plain ASCII is refused,
  since Python and curl may turn it into different ASCII names.
"""
import ipaddress
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .outbound import OutboundError, _resolve_public

#: What a host can be asked to fetch from, and the port each defaults to.
ALLOWED_SCHEMES = {"https": 443, "sftp": 22, "ftp": 21}

#: What a host name may hold: anything else (a percent sign, a space, a
#: backslash) Python and the host's curl need not read alike.
_HOST_NAME = re.compile(r"[a-z0-9._-]+")


def split_credentials(url):
    """Separate any credentials from *url*.

    An ``@``, a ``:`` or a ``/`` can only be written into a link escaped
    (``%40``): the values are returned unescaped, as the server expects
    them.

    :param url: candidate URL, possibly carrying ``user:password@``.
    :return: ``(url_without_credentials, username, password)``.
    """
    parts = urlsplit((url or "").strip())
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if not (username or password):
        return (url or "").strip(), "", ""
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((
        parts.scheme, netloc, parts.path, parts.query, parts.fragment,
    )), username, password


def masked(url):
    """Return *url* with any credentials replaced by a placeholder.

    This is the only form that is allowed to be stored or displayed.

    :param url: candidate URL.
    :return: the URL with ``user:***@`` in place of the secret.
    """
    parts = urlsplit((url or "").strip())
    if not (parts.username or parts.password):
        return (url or "").strip()
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    user = quote(parts.username or "", safe="")
    return urlunsplit((
        parts.scheme, f"{user}:***@{host}", parts.path, parts.query,
        parts.fragment,
    ))


def validate(url):
    """Check *url* is something a host may be told to download.

    :param url: candidate URL, credentials already split off.
    :return: ``(parts, pinned_address, port)`` — the pin is what stops
        the name resolving elsewhere by the time the host dials it.
    :raise OutboundError: with a reason safe to show the user.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ALLOWED_SCHEMES:
        raise OutboundError(
            "the URL must start with " + ", ".join(
                f"{scheme}://" for scheme in sorted(ALLOWED_SCHEMES)
            )
        )
    if parts.username or parts.password:
        raise OutboundError("credentials must be given separately")
    host = parts.hostname
    if not host:
        raise OutboundError("the URL has no host")
    # Python (IDNA2003) and the hosts' curl (IDNA2008) can turn the same
    # name into two different ASCII names: the one checked here would
    # not be the one fetched there.
    if not host.isascii():
        raise OutboundError(
            "the host name must be plain ASCII: write an internationalised "
            "name in its punycode form (xn--…)"
        )
    if not _HOST_NAME.fullmatch(host) and not _is_ip_literal(host):
        raise OutboundError("the host name holds characters no name holds")
    port = parts.port or ALLOWED_SCHEMES[parts.scheme]
    if not 0 < port < 65536:
        raise OutboundError("the URL has an impossible port")
    address = _resolve_public(host, port)
    return parts, address, port


def _is_ip_literal(host):
    """Return whether *host* is an IP address rather than a name.

    :param str host: the URL's host, without brackets
    :rtype: bool
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def curl_url(parts):
    """Return the link curl is given, rebuilt from what was validated.

    Its host is :func:`validate`'s, byte for byte the one
    :func:`curl_resolve_argument` pins: a host written otherwise in the
    link (another case, characters Python's parser dropped) would not
    match the pin, and curl would look the name up itself. A fragment is
    never sent anyway, so it is left out.

    :param parts: result of :func:`validate`.
    :return: ``scheme://host[:port]/path[?query]``
    :rtype: str
    """
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def curl_resolve_argument(parts, address, port):
    """Return the ``--resolve`` argument pinning the validated address.

    :param parts: result of :func:`validate`.
    :param address: address the name resolved to here.
    :param port: port the transfer will use.
    :return: ``host:port:address`` in the shape curl expects.
    """
    literal = address
    with_brackets = ipaddress.ip_address(address).version == 6
    if with_brackets:
        literal = f"[{address}]"
    return f"{parts.hostname}:{port}:{literal}"
