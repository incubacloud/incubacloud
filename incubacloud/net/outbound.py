"""The one door outbound HTTP takes when a user chose the destination.

Everywhere else in this codebase the remote host is ours or a fixed
vendor (``api.github.com``, ``api.telegram.org``). Notification webhooks
are the exception: the URL comes from a user, and the request leaves the
manager — a machine that reaches the cloud metadata service, its own
loopback Odoo and the internal Postgres. Measured on 2026-08-22 from the
production container: 169.254.169.254:80, 127.0.0.1:8069 and db:5432 all
answer. There is no egress filter to fall back on.

The old code checked ``url.startswith('https://')`` when the value was
saved and then handed it to a bare ``urllib.request.urlopen``. That
check is defeated three ways:

* **Redirects.** ``urllib`` follows them by default and permits the
  https -> http downgrade, so one 302 reaches any internal address.
* **DNS.** A public-looking hostname can resolve to 127.0.0.1.
* **Time.** Even a correct check at save time says nothing about where
  the name points minutes later, when the notification is actually sent.

So validation happens here, at send time, and the socket is pinned to
the address that was validated. Resolving, approving and then letting
the stack resolve again is the classic TOCTOU that DNS rebinding is
built to exploit — the pin is what makes the check mean anything.

Redirects are refused outright rather than re-validated. It is the
stronger answer and it is what this codebase already does for GitHub
(:mod:`..github.http_utils`); a webhook receiver that needs a redirect
can publish the final URL instead.

The response is read and discarded on purpose. Nothing about it reaches
the caller, so this stays a blind channel: no reflected body, no status,
no error detail to use as an oracle.
"""
import http.client
import ipaddress
import ssl
# Imported by name, not through the module: patching
# ``socket.getaddrinfo`` reaches the shared stdlib module and takes DNS
# away from the whole process, test client included. These module-local
# names give tests a seam that only affects this file.
from socket import IPPROTO_TCP, create_connection, gaierror, getaddrinfo
from urllib.parse import urlsplit

# Only 443. A webhook receiver on another port is possible in theory and
# has never existed here in practice, and every extra port widens what an
# internal scan can reach.
_ALLOWED_PORT = 443

# Enough for the handshake plus a small POST; the receiver's answer is
# discarded, so nothing here waits on a slow body beyond this.
_DEFAULT_TIMEOUT = 10

# The reply is thrown away. Read a bounded amount anyway so a hostile
# receiver cannot hold the worker with an endless stream.
_MAX_RESPONSE_BYTES = 8192


class OutboundError(Exception):
    """A request was refused before it left, or could not complete.

    Carries no remote detail beyond what the caller already knows, so
    logging it cannot turn the webhook into a probe of the internal
    network.
    """


def _reject_address(ip):
    """Return a reason string when *ip* must not be dialled, else ''.

    Fail-closed: anything that is not unambiguously a public unicast
    address is refused. ``is_global`` alone is not enough — it answers
    False for the ranges we care about but the explicit checks below
    keep the intent readable and survive a stdlib definition change.

    :param ip: ``ipaddress.IPv4Address`` or ``IPv6Address``.
    :returns: human-readable reason, or '' when the address is allowed.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        # ::ffff:127.0.0.1 reaches loopback while looking like IPv6.
        return _reject_address(ip.ipv4_mapped)
    if ip.is_loopback:
        return 'loopback'
    if ip.is_link_local:
        # Includes 169.254.169.254, the cloud metadata service.
        return 'link-local'
    if ip.is_unspecified:
        # Checked before ``is_private``: 0.0.0.0/8 sits inside the
        # private ranges, so the vaguer reason would shadow this one.
        return 'unspecified'
    if ip.is_private:
        return 'private'
    if ip.is_reserved:
        return 'reserved'
    if ip.is_multicast:
        return 'multicast'
    if not ip.is_global:
        return 'not globally routable'
    return ''


def _resolve_public(host, port):
    """Resolve *host* and return one address safe to dial.

    Every address the name resolves to must pass. A name that answers
    with one public and one private address is refused outright: which
    one the connection would have used is not ours to decide, and
    accepting the pair is how a rebinding host gets a second chance.

    :param host: hostname or IP literal from the URL.
    :param port: TCP port, already validated.
    :returns: the address string to connect to.
    :raises OutboundError: the name does not resolve, or any address
        it resolves to is not public.
    """
    try:
        infos = getaddrinfo(host, port, proto=IPPROTO_TCP)
    except gaierror as exc:
        raise OutboundError(f'cannot resolve {host!r}') from exc
    if not infos:
        raise OutboundError(f'{host!r} resolves to nothing')
    addresses = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise OutboundError(f'{host!r} resolved to {raw!r}') from exc
        reason = _reject_address(ip)
        if reason:
            raise OutboundError(
                f'{host!r} resolves to a {reason} address',
            )
        addresses.append(raw)
    return addresses[0]


def validate_url(url):
    """Check *url* is a webhook destination we are willing to dial.

    Runs at save time so the user hears about a bad URL immediately,
    and again inside :func:`post_json` because the answer can change
    between the two.

    :param url: the candidate URL.
    :returns: the parsed ``SplitResult``, for callers that go on to
        build the request; most only care that it did not raise.
    :raises OutboundError: with a reason safe to show the user.
    """
    parts = urlsplit(url)
    if parts.scheme != 'https':
        raise OutboundError('the URL must start with https://')
    if parts.username or parts.password:
        # https://real-host@127.0.0.1/ reads as the real host to a
        # human and dials the loopback.
        raise OutboundError('the URL must not carry credentials')
    host = parts.hostname
    if not host:
        raise OutboundError('the URL has no host')
    port = parts.port or 443
    if port != _ALLOWED_PORT:
        raise OutboundError(f'the URL must use port {_ALLOWED_PORT}')
    _resolve_public(host, port)
    return parts


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed address, still speaking the hostname's name.

    ``self.host`` stays the hostname so SNI and the ``Host`` header —
    and therefore certificate validation — are unchanged; only the
    address the socket dials is replaced. That is the whole point: the
    address is the one that passed validation, not whatever DNS answers
    on the second lookup the stack would otherwise make.
    """

    def __init__(self, host, pinned_ip, **kwargs):
        """Bind an HTTPS connection for *host* to *pinned_ip*.

        :param host: hostname, used for SNI, Host and cert checking.
        :param pinned_ip: literal address the socket connects to.
        """
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip
        # ``HTTPConnection.__init__`` assigns ``self._create_connection =
        # socket.create_connection`` as an *instance attribute*, which
        # shadows a subclass method of the same name — the override would
        # never run and the socket would quietly resolve the hostname
        # again, which is the exact window this class exists to close.
        # Rebinding here is what actually pins it.
        self._create_connection = self._dial_pinned

    def _dial_pinned(self, address, *args, **kwargs):
        """Dial the pinned address, keeping the port the caller asked for.

        :param address: ``(host, port)`` the stack wanted to dial; only
            the port is honoured.
        :returns: the connected socket.
        """
        return create_connection(
            (self._pinned_ip, address[1]), *args, **kwargs
        )


def post_json(url, payload, headers=None, timeout=_DEFAULT_TIMEOUT):
    """POST *payload* to *url*, or refuse to.

    :param url: destination, re-validated here regardless of any earlier
        check.
    :param payload: request body, already encoded to bytes.
    :param headers: extra headers; ``Content-Type`` is set for the
        caller when absent.
    :param timeout: seconds for the whole exchange.
    :raises OutboundError: the destination was refused, or the exchange
        failed. Callers log it; nothing from the remote end is included.
    """
    parts = validate_url(url)
    host = parts.hostname
    port = parts.port or _ALLOWED_PORT
    pinned = _resolve_public(host, port)
    path = parts.path or '/'
    if parts.query:
        path = f'{path}?{parts.query}'
    sent = {'Content-Type': 'application/json', **(headers or {})}
    conn = _PinnedHTTPSConnection(
        host,
        pinned,
        port=port,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        conn.request('POST', path, body=payload, headers=sent)
        response = conn.getresponse()
        # Drain a bounded amount so the socket closes cleanly, then
        # drop it. The caller learns nothing about the far side.
        response.read(_MAX_RESPONSE_BYTES)
    except OSError as exc:
        raise OutboundError(f'delivery to {host} failed') from exc
    finally:
        conn.close()
