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
  the job payload, the log and the interface is :func:`shown_link`; the
  secret reaches the host as a file, never as a word in a command line
  that ``ps`` would show.
* the resolved address is pinned and handed to ``curl``, so the name
  cannot resolve to a public address here and a private one there. The
  link curl gets is rebuilt from the name checked (``curl_url``), so the
  pin always matches it; a name that is not plain ASCII is refused,
  since Python and curl may turn it into different ASCII names.

A link is never stored or shown whole: a presigned link carries its
signature in the query, a share link its token in the path. The job
keeps it encrypted (``cloud.job.secret_payload``); what is stored and
shown is :func:`shown_link`, and what a person approves is
:func:`told_link`.
"""
import ipaddress
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from .outbound import OutboundError, _resolve_public

#: What a host can be asked to fetch from, and the port each defaults to.
ALLOWED_SCHEMES = {"https": 443, "sftp": 22, "ftp": 21}

#: A piece of a link (a path segment, a value of its query) as long as
#: this is shown whole: a word of the path (``s``, ``download``, ``uc``)
#: or a short value (``dl=1``).
SHOWN_WHOLE = 8
#: Characters of a longer piece always left out of what is shown, so
#: that what is shown never suffices to download: 8 even of hexadecimal
#: are 4 thousand million guesses. At most 4 are shown at each end.
HIDDEN_AT_LEAST = 8
SHOWN_AT_EACH_END = 4

#: An extension kept apart from the name it ends, so ``backup.zip``
#: still reads as a zip whatever its name is cut to.
_EXTENSION = re.compile(r"(.+)(\.[A-Za-z0-9]{1,5})")

#: What a host name may hold: anything else (a percent sign, a space, a
#: backslash) Python and the host's curl need not read alike.
_HOST_NAME = re.compile(r"[a-z0-9._-]+")

#: Names of the query of a presigned link whose value says who signed it:
#: an access key id, with its date, region and service (S3's signature
#: versions 4 and 2, Google Cloud Storage's V4 and V2, Alibaba's OSS). Not
#: a secret — the signature is — and what tells one account's link from
#: another's: shown whole where a person approves a link. Compared
#: without case, as the services read them.
SIGNER_PARAMS = frozenset({
    "x-amz-credential",
    "awsaccesskeyid",
    "x-goog-credential",
    "googleaccessid",
    "ossaccesskeyid",
})

#: Hosts of the object storages that name the bucket in the first
#: segment of the path (path-style): S3 and its regional and dual-stack
#: endpoints, Google Cloud Storage, and the S3-compatible ones links of
#: backups usually come from. The bucket is no secret, and an attacker
#: chooses one that reads like the user's (``acme-backups-prod``,
#: ``acme-evil-xprod``): it is shown whole.
_PATH_STYLE_HOST = re.compile(
    r"(?:s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com(?:\.cn)?"
    r"|storage\.googleapis\.com|storage\.cloud\.google\.com"
    r"|[a-z0-9-]+\.digitaloceanspaces\.com"
    r"|s3\.[a-z0-9.-]+\.(?:wasabisys\.com|backblazeb2\.com|scw\.cloud"
    r"|cloud\.ovh\.net)"
    r"|[a-z0-9-]+\.your-objectstorage\.com"
    r"|[a-z0-9]+\.r2\.cloudflarestorage\.com)"
)


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


def cut(piece):
    """Return a piece of a link as it may be shown: its ends at most.

    A piece of :data:`SHOWN_WHOLE` characters or fewer is shown whole;
    a longer one keeps at most :data:`SHOWN_AT_EACH_END` characters at
    each end around ``…``, and never fewer than :data:`HIDDEN_AT_LEAST`
    are left out. An extension (``.zip``) is kept after the cut name.

    :param str piece: a path segment or a value of the query, as written
    :rtype: str
    """
    piece = piece or ""
    if len(piece) <= SHOWN_WHOLE:
        return piece
    extension = ""
    named = _EXTENSION.fullmatch(piece)
    if named:
        piece, extension = named.groups()
        if len(piece) <= SHOWN_WHOLE:
            return piece + extension
    ends = min(SHOWN_AT_EACH_END, (len(piece) - HIDDEN_AT_LEAST) // 2)
    if ends <= 0:
        return "…" + extension
    return f"{piece[:ends]}…{piece[-ends:]}{extension}"


def _shown_host(parts):
    """Return ``scheme://host[:port]`` of a split link, without any user.

    :param parts: ``urlsplit`` of the link
    :rtype: str
    """
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}"


def shown_link(url):
    """Return a link as the panel stores or reports it: host and file.

    The scheme, the host and the last segment of the path, cut
    (:func:`cut`); the rest of the path becomes ``…``, a query ``?…``,
    and any user or password goes. A presigned link carries its
    signature in the query, a share link its token in the path, often
    as its last segment (``we.tl/t-…``): neither is kept whole. What a
    job's payload, the audit and an agent's answer hold.

    :param str url: the link as given
    :rtype: str
    """
    parts = urlsplit((url or "").strip())
    segments = [segment for segment in parts.path.split("/") if segment]
    path = "/" + (cut(segments[-1]) if segments else "")
    if len(segments) > 1:
        path = "/…" + path
    return f"{_shown_host(parts)}{path}{'?…' if parts.query else ''}"


def _signed_by_storage(pairs):
    """Return whether a query, as ``(name, value)`` pairs, is a storage's presign.

    :param pairs: the query's names and values, as written
    :rtype: bool
    """
    return any(name.lower() in SIGNER_PARAMS for name, _value in pairs)


def told_link(url):
    """Return a link as a person approves it: every piece, cut.

    Host and port whole; each segment of the path and each value of the
    query cut (:func:`cut`), the names of the query whole. Two links of
    one host differ, then, wherever they differ by more than what is cut
    away — a share link of another token — and what is shown never
    suffices to download.

    What tells whose a presigned link is, and is no secret, is shown
    whole: who signed it (:data:`SIGNER_PARAMS`), and the bucket, the
    first segment of the path, on a storage that names it there
    (:data:`_PATH_STYLE_HOST`) or on any host the link's query says a
    storage signed — an S3-compatible one of its own. An attacker
    chooses a bucket that reads like the user's once cut; a signature,
    or a share link's token, stays cut.

    :param str url: the link as given
    :rtype: str
    """
    parts = urlsplit((url or "").strip())
    pairs = [
        (name, value) if sep else (name, None)
        for name, sep, value in (
            pair.partition("=") for pair in parts.query.split("&") if pair
        )
    ]
    bucket_first = bool(
        _PATH_STYLE_HOST.fullmatch(parts.hostname or "")
        or _signed_by_storage(pairs)
    )
    segments = parts.path.split("/")
    path = "/".join(
        segment if bucket_first and index == 1 else cut(segment)
        for index, segment in enumerate(segments)
    )
    query = "&".join(
        cut(name) if value is None
        else f"{name}={value if name.lower() in SIGNER_PARAMS else cut(value)}"
        for name, value in pairs
    )
    return f"{_shown_host(parts)}{path}{'?' + query if query else ''}"


def validate(url, only_port=None, refused=(), ipv4_only=False):
    """Check *url* is something a host may be told to download.

    :param url: candidate URL, credentials already split off.
    :param int only_port: the one port the link may use (an agent's
        link: 443), or None for any.
    :param refused: networks (``ipaddress`` networks) the address may
        not be in, however public: the panel's own servers, for a link
        an agent gives. An IPv4-mapped address (``::ffff:a.b.c.d``) is
        compared as the IPv4 address it reaches.
    :param bool ipv4_only: pin an IPv4 address only, and refuse a link
        that has none: the panel knows its servers' IPv4 addresses, not
        their IPv6 ones, so *refused* cannot speak for an IPv6 address
        (an agent's link).
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
    try:
        port = parts.port or ALLOWED_SCHEMES[parts.scheme]
    except ValueError as exc:
        raise OutboundError("the URL has an impossible port") from exc
    if not 0 < port < 65536:
        raise OutboundError("the URL has an impossible port")
    if only_port and port != only_port:
        raise OutboundError(f"the URL must use port {only_port}")
    if ipv4_only and _is_ip_literal(host) \
            and ipaddress.ip_address(host).version == 6:
        raise OutboundError(
            "the URL must name its server by an IPv4 address or by a name "
            "with one"
        )
    address = _resolve_public(host, port, version=4 if ipv4_only else None)
    ip = ipaddress.ip_address(address)
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if any(ip in network for network in refused if network.version == ip.version):
        raise OutboundError(
            "the URL names a server of this panel: copy the archive from it "
            "another way"
        )
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
