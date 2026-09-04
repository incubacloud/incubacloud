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
  cannot resolve to a public address here and a private one there.
"""
import ipaddress
from urllib.parse import quote, urlsplit, urlunsplit

from .outbound import OutboundError, _resolve_public

#: What a host can be asked to fetch from, and the port each defaults to.
ALLOWED_SCHEMES = {"https": 443, "sftp": 22, "ftp": 21}


def split_credentials(url):
    """Separate any credentials from *url*.

    :param url: candidate URL, possibly carrying ``user:password@``.
    :return: ``(url_without_credentials, username, password)``.
    """
    parts = urlsplit((url or "").strip())
    username = parts.username or ""
    password = parts.password or ""
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
    port = parts.port or ALLOWED_SCHEMES[parts.scheme]
    if not 0 < port < 65536:
        raise OutboundError("the URL has an impossible port")
    address = _resolve_public(host, port)
    return parts, address, port


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
