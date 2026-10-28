"""Who Odoo thinks the visitor is, when a proxy stands in front.

Odoo reads the visitor's address from ``X-Forwarded-For``, and it reads
the **last** entry: ``werkzeug``'s ``ProxyFix`` is constructed with
``x_for=1`` in ``odoo/http.py``, which is not configurable.

That is right for one proxy and wrong for two. A reverse proxy appends
the address it received the connection from before passing the request
on — measured on Traefik v2.11, and after its middlewares have run, so
nothing configured inside the proxy can change what ends up last. Put a
CDN in front of that proxy and the chain reads ``<visitor>, <edge>``:
the last entry is the CDN's edge, and every visitor of the site
collapses onto the handful of addresses it owns.

Nothing reports this. The site works, sessions open, and the address
recorded against each one is simply not the visitor's.

The CDN does say who the visitor is, in a header of its own. This turns
that assertion into the one Odoo already reads, and only when the CDN
is provably the one making it — an unproven header is written by
whoever is calling, so honouring one from anybody else would let any
visitor claim to be any address.

Proof takes two facts, because there are two hops. The connection to
Odoo has to be our own proxy's, and the last entry of the chain — the
one that proxy appends from the connection it accepted — has to be one
of the CDN's addresses. Neither a CDN header nor any earlier entry can
stand in for that entry: a proxy keeps what a peer sends only if it
trusts that peer, and always appends the peer itself last — measured on
Traefik v2.11 and v3.1, which also pass the CDN's header through from
anybody. A CDN connecting to Odoo itself, with no proxy of ours in
between, is believed too: its edge is then the connection.

The CDN is somebody on the internet. Its networks are read from
``x_real_ip_trusted`` keeping only public ones, and an address is taken
for the CDN's only if it is public itself, so that no list — a host's
own ``trusted_proxy_ranges`` wins over the CDN's and may name a private
network, or one as wide as ``0.0.0.0/0`` — lets anything on Odoo's own
network speak as the CDN.

How much the rest proves depends on how the proxy is recognised:

- **Named** in ``x_real_ip_proxy`` — a container name or an address —
  only connections from it count as the proxy. Nobody else on Odoo's
  network can set the visitor's address: not with the CDN's header, and
  not with ``X-Forwarded-For`` either, which is replaced by the
  caller's own address before Odoo reads it. This is how the master
  runs, and every panel once deployed again: their Odoo shares one
  Docker network with Traefik, with the other panels of the host and,
  on the master, with QA. A panel deployed before the option existed
  runs unnamed until its next deploy or plan apply.
- **Unnamed**, any connection from this machine or a private network
  is taken for the proxy. A caller from a public address cannot choose
  its address; anything on a private network Odoo shares can, exactly
  as Odoo's own ``proxy_mode`` already lets it with ``X-Forwarded-For``
  alone. Where Odoo shares its network with other containers, name the
  proxy (see ``docs/runbooks/RB-25-visitor-address-behind-a-cdn.md``).

A named proxy whose name stops resolving does not leave Odoo believing
nobody — every visitor would then be recorded as the proxy, one address
for every limit keyed on it. It falls back to unnamed, says so in the
log, and goes back to the name once it resolves (see :class:`OwnProxy`).
A server that never serves a proxied request — the job runner, which
loads the same configuration away from Traefik's network — only warns
about it as it starts.

Until 2026-10-27 the proof was the connection to Odoo being the CDN's.
It never is with a proxy in between — it is the proxy's container — so
nothing was ever corrected, on the master or on any panel.

Deliberately free of any particular CDN or proxy: the header name, the
addresses to believe and the proxy's name are given by the caller.
"""
import ipaddress
import logging
import socket
from socket import getaddrinfo
from time import monotonic

_logger = logging.getLogger(__name__)

#: Config keys read from the Odoo configuration file. Read from there
#: rather than from a record because this runs before any registry
#: exists — it is installed while the module is imported.
HEADER_OPTION = "x_real_ip_header"
TRUSTED_OPTION = "x_real_ip_trusted"
#: Who our own proxy is: container names or addresses, separated by
#: commas or spaces. A name ending in a dot is absolute: the resolver
#: does not try it under any search domain. Optional; see
#: :class:`OwnProxy` for what leaving it out means.
PROXY_OPTION = "x_real_ip_proxy"

#: Seconds an answer for the proxy's name is reused. Docker answers from
#: memory, but this sits on the path of every proxied request.
PROXY_TTL_SECONDS = 30.0
#: Seconds that must pass before a connection from an address the last
#: answer did not include asks again. Short, so a proxy recreated on a
#: new address is recognised at once; not zero, so a neighbour cannot
#: turn each of its requests into a lookup.
PROXY_RETRY_SECONDS = 5.0
#: Seconds the last good answer outlives the name failing to resolve,
#: whatever the error: which one a missing container gets depends on
#: Docker's version (one that no longer exists was answered "try again"
#: on 2026-10-27), so it says nothing about whether the proxy is gone.
#: Long enough for a restart; short enough that a proxy renamed for good
#: does not keep its old address for ever. Meanwhile that address counts
#: as the proxy even if Docker has already handed it to another container
#: on the network — still narrower than the fallback, where every
#: private address does.
PROXY_STALE_SECONDS = 300.0
#: Seconds between two errors logged about a name that does not resolve.
PROXY_COMPLAINT_SECONDS = 600.0

#: Where an unnamed proxy is taken to connect to Odoo from: this
#: machine, or a private network such as the one Docker puts the
#: proxy's container on. Public addresses are left out because whatever
#: connects from one writes the whole chain itself. What this cannot
#: tell apart is the proxy from anything else on the same private
#: network — which is what naming the proxy is for.
OWN_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8", "::1/128",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "fc00::/7",
    )
)


def environ_key(header):
    """Return the WSGI environ key carrying HTTP header *header*.

    :param str header: a header name, e.g. ``CF-Connecting-IP``
    :rtype: str
    """
    return "HTTP_" + (header or "").strip().upper().replace("-", "_")


def parse_networks(raw):
    """Return the networks named in *raw*, dropping what is unusable.

    Separated by commas or whitespace. An entry that is not a network
    is skipped rather than raising: this runs at import time, in the
    server process, and a typo in a configuration file must not stop
    Odoo from starting. It does mean a typo silently narrows who is
    believed, which is the safe direction — the header is then ignored
    and the address stays the proxy's.

    :param str raw: comma or space separated CIDRs
    :rtype: list
    """
    networks = []
    for chunk in (raw or "").replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            _logger.warning(
                "[real-ip] ignoring %r: not a network", chunk,
            )
    return networks


def cdn_networks(raw):
    """Return the CDN's networks named in *raw*: the public ones.

    A CDN answers from the internet. A private network in its list —
    which a host's own ``trusted_proxy_ranges`` can put there, since it
    wins over the CDN's — would let anything on Odoo's own network speak
    as the CDN, so it is dropped and said so.

    :param str raw: comma or space separated CIDRs
    :rtype: list
    """
    networks = []
    for network in parse_networks(raw):
        if network.is_global:
            networks.append(network)
        else:
            _logger.warning(
                "[real-ip] ignoring %s: not a public network, so no CDN's",
                network,
            )
    return networks


def _address(value):
    """Return *value* as an address, or ``None`` when it is not one.

    A dual-stack socket reports an IPv4 peer as ``::ffff:a.b.c.d``; it
    is returned as the IPv4 address it is, so that it compares equal to
    one and falls inside IPv4 networks.

    :param str value: an address as text
    :rtype: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    """
    try:
        ip = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _private(ip):
    """Return whether *ip* is on this machine or a private network.

    :param ip: an address, as :func:`_address` returns it
    :rtype: bool
    """
    return any(ip in network for network in OWN_PROXY_NETWORKS)


def is_trusted(address, networks):
    """Return whether *address* falls inside any of *networks*.

    :param str address: an address, from a connection or the chain
    :param networks: networks to look it up in
    :rtype: bool
    """
    ip = _address(address)
    if ip is None:
        return False
    return any(ip in network for network in networks)


def _from_cdn(address, networks):
    """Return whether *address* is a public address of the CDN's.

    :param str address: an address, from a connection or the chain
    :param networks: the CDN's networks
    :rtype: bool
    """
    ip = _address(address)
    return (
        ip is not None
        and ip.is_global
        and any(ip in network for network in networks)
    )


class OwnProxy:
    """Recognise the connections our own proxy opens to Odoo.

    **Named** — ``x_real_ip_proxy`` holds the proxy's container names or
    addresses — only those count. A name is asked of the resolver
    Docker gives every container, which answers with the container's
    address on the network both share; the answer is reused for
    :data:`PROXY_TTL_SECONDS`, and asked again early, no sooner than
    :data:`PROXY_RETRY_SECONDS` after the last time, when a connection
    comes from an address it does not include.

    The name may stop resolving. After a good answer, the last one is
    kept for :data:`PROXY_STALE_SECONDS`, whatever the error. A name
    that never resolved, or one still unresolved past that, falls back
    to unnamed — the weaker guarantee, never "nobody", which would
    record every visitor as the proxy — and is asked again every
    :data:`PROXY_RETRY_SECONDS` while a request comes in. The name
    counts again as soon as it resolves.

    Said as the server starts with a warning (:meth:`look_up`): a
    server that never serves a proxied request, such as the job runner
    away from Traefik's network, has nothing to fix. Said with an error,
    once every :data:`PROXY_COMPLAINT_SECONDS`, when a request finds the
    fallback in use.

    **Unnamed**, any connection from this machine or a private network
    counts (:data:`OWN_PROXY_NETWORKS`). On a network Odoo shares with
    other containers that includes all of them.

    The name is not what makes this hold on a shared network: any
    project there could declare the same name as an alias. What does is
    that only the platform writes compose files onto that network; the
    name rules out picking the wrong container by accident.
    """

    def __init__(self, raw=""):
        """Read the proxy's names and addresses from *raw*.

        :param str raw: the value of ``x_real_ip_proxy``; empty for an
            unnamed proxy
        """
        self.entries = tuple((raw or "").replace(",", " ").split())
        self.addresses = frozenset(
            ip for ip in map(_address, self.entries) if ip is not None
        )
        self.names = tuple(
            entry for entry in self.entries if _address(entry) is None
        )
        self._answer = frozenset()
        self._good = False
        self._unnamed = False
        self._asked_at = None
        self._failing_since = None
        self._complained_at = None

    def __str__(self):
        """Say who the proxy is, for the line logged at start-up."""
        if not self.entries:
            return "any proxy on this machine or a private network"
        return ", ".join(self.entries)

    def _ask(self):
        """Ask the resolver for the names, now.

        :return: ``(addresses, reason)`` — what resolved, and what went
            wrong, for the log
        :rtype: tuple
        """
        found, reasons = set(), []
        for name in self.names:
            try:
                answers = getaddrinfo(name, None, type=socket.SOCK_STREAM)
            except socket.gaierror as error:
                reasons.append(f"{name}: {error.strerror or error}")
                continue
            except (OSError, UnicodeError) as error:
                reasons.append(f"{name}: {error}")
                continue
            for answer in answers:
                # A link-local answer carries its zone after ``%``.
                ip = _address(str(answer[4][0]).split("%")[0])
                if ip is not None:
                    found.add(ip)
        return frozenset(found), "; ".join(reasons)

    def _refresh(self, now, starting=False):
        """Ask again, and decide what counts until the next time.

        :param float now: the module's clock, read by the caller
        :param bool starting: asked as the server starts, where falling
            back is only warned about and does not count against the
            next error
        """
        found, reason = self._ask()
        self._asked_at = now
        if found:
            self._answer, self._good, self._unnamed = found, True, False
            self._failing_since = None
            return
        if self._failing_since is None:
            self._failing_since = now
        if self._good and now - self._failing_since < PROXY_STALE_SECONDS:
            return
        self._unnamed = True
        if starting:
            _logger.warning(
                "[real-ip] the proxy %s does not resolve here (%s): any "
                "connection from this machine or a private network counts "
                "as the proxy until it does. Expected where no proxied "
                "request arrives, such as the job runner",
                self, reason,
            )
            return
        if (self._complained_at is None
                or now - self._complained_at >= PROXY_COMPLAINT_SECONDS):
            self._complained_at = now
            _logger.error(
                "[real-ip] the proxy %s does not resolve (%s): until it "
                "does, any connection from this machine or a private "
                "network counts as the proxy",
                self, reason,
            )

    def _due(self, ip, now):
        """Return whether the names have to be asked again for *ip*.

        :param ip: the address about to be looked up in the answer
        :param float now: the module's clock, read by the caller
        :rtype: bool
        """
        if self._asked_at is None:
            return True
        age = now - self._asked_at
        if age >= PROXY_TTL_SECONDS:
            return True
        return age >= PROXY_RETRY_SECONDS and (
            self._unnamed or ip not in self._answer
        )

    def look_up(self):
        """Ask for the names now, as the server starts.

        So that a name that will not resolve is in the log before the
        first request is recorded under the fallback — as a warning: the
        same configuration starts servers that never see one.

        :return: whether the names, rather than the fallback, decide
        :rtype: bool
        """
        if not self.names:
            return True
        self._refresh(monotonic(), starting=True)
        return not self._unnamed

    def is_ours(self, address):
        """Return whether a connection from *address* is our proxy's.

        :param str address: the address the connection came from
        :rtype: bool
        """
        ip = _address(address)
        if ip is None:
            return False
        if not self.entries:
            return _private(ip)
        if ip in self.addresses:
            return True
        if not self.names:
            return False
        now = monotonic()
        if self._due(ip, now):
            self._refresh(now)
        if self._unnamed:
            return _private(ip)
        return ip in self._answer


#: The proxy when ``x_real_ip_proxy`` is not set. Holds no state.
_UNNAMED = OwnProxy()


def last_hop(environ):
    """Return the address our proxy says it was reached from.

    The last entry of ``X-Forwarded-For``. A reverse proxy appends the
    address of the connection it accepted, after discarding whatever
    chain an untrusted peer sent, so this entry comes from a socket and
    not from the caller — provided the proxy is ours, which is for
    :func:`asserted_client` to establish.

    :param dict environ: the WSGI environment
    :return: that address, or ``''`` when there is no chain
    :rtype: str
    """
    chain = (environ.get("HTTP_X_FORWARDED_FOR") or "").split(",")
    entries = [entry.strip() for entry in chain if entry.strip()]
    return entries[-1] if entries else ""


def _speaker(environ, networks, proxy):
    """Return who connected to Odoo, if it may speak for somebody else.

    :param dict environ: the WSGI environment
    :param networks: the CDN's networks
    :param OwnProxy proxy: who our proxy is
    :return: ``'proxy'`` for our own proxy, ``'cdn'`` for the CDN
        connecting directly, ``None`` for anybody else
    :rtype: str | None
    """
    remote = environ.get("REMOTE_ADDR")
    if proxy.is_ours(remote):
        return "proxy"
    if _from_cdn(remote, networks):
        return "cdn"
    return None


def _claim(environ, header, networks, speaker):
    """Return the visitor the CDN names, if *speaker* proves it is the CDN.

    :param dict environ: the WSGI environment
    :param str header: header the CDN states the visitor in
    :param networks: the CDN's networks
    :param str speaker: what :func:`_speaker` returned
    :return: the visitor's address, written the one way it is written
        (an IPv4 address the way IPv4 is, IPv6 compressed and in lower
        case), or ``None``
    :rtype: str | None
    """
    if speaker is None:
        return None
    if speaker == "proxy" and not _from_cdn(last_hop(environ), networks):
        return None
    claimed = (environ.get(environ_key(header)) or "").split(",")[0].strip()
    # ``fe80::1%eth0`` parses as an address, and the zone after ``%`` is
    # whatever the sender typed.
    if "%" in claimed:
        return None
    ip = _address(claimed)
    return str(ip) if ip is not None else None


def asserted_client(environ, header, networks, proxy=None):
    """Return the visitor address the CDN claims, or ``None``.

    Believed when our own proxy connected to Odoo and was itself reached
    from a public address in *networks*, or when the CDN connected to
    Odoo directly from one. ``None`` covers every case where nothing
    should change: no header configured, no networks to believe, a
    connection to Odoo from anybody else, a proxy reached by anybody but
    the CDN, a header that is absent, holds something that is not an
    address, or carries a zone.

    Only the first entry is read. The header is the CDN's own assertion
    about its client; anything after a comma was appended by somebody
    further out and is not the CDN speaking.

    :param dict environ: the WSGI environment
    :param str header: header the CDN states the visitor in
    :param networks: the CDN's networks
    :param OwnProxy proxy: who our proxy is; unnamed when not given
    :rtype: str | None
    """
    if not (header and networks):
        return None
    speaker = _speaker(environ, networks, proxy or _UNNAMED)
    return _claim(environ, header, networks, speaker)


def rewrite(environ, header, networks, proxy=None):
    """Leave in the forwarded chain only what Odoo may believe.

    When the CDN is proven to speak, the visitor it names becomes the
    whole chain. Rewritten rather than appended: the chain becomes what
    it would have been with the proxy as the only hop, which is what the
    entry Odoo reads is supposed to mean, and keeping the rest would
    leave the edge's address in a position that claims to be a client.

    When the caller is neither our proxy nor the CDN, its chain becomes
    its own address. Odoo's ``proxy_mode`` believes the last entry from
    whoever connects, so without this a caller on Odoo's network would
    choose its address by sending ``X-Forwarded-For`` alone. A request
    without a chain is left without one.

    :param dict environ: the WSGI environment, changed in place
    :param str header: header the CDN states the visitor in
    :param networks: the CDN's networks
    :param OwnProxy proxy: who our proxy is; unnamed when not given
    :return: the visitor written, or ``None`` when none was
    :rtype: str | None
    """
    if not (header and networks):
        return None
    speaker = _speaker(environ, networks, proxy or _UNNAMED)
    client = _claim(environ, header, networks, speaker)
    if client:
        environ["HTTP_X_FORWARDED_FOR"] = client
        return client
    if speaker is None and "HTTP_X_FORWARDED_FOR" in environ:
        environ["HTTP_X_FORWARDED_FOR"] = environ.get("REMOTE_ADDR") or ""
    return None


def uninstalled(proxy_fix):
    """Return *proxy_fix* as it was before this module wrapped it.

    The identity when nothing wrapped it, so a caller does not have to
    know whether the correction is installed — which depends on the
    configuration of whichever installation it is running on.

    :rtype: callable
    """
    return getattr(proxy_fix, "_incubacloud_wrapped", proxy_fix)


def install(config, http_module):
    """Have Odoo read the visitor's address instead of the proxy's.

    Wraps the name ``odoo.http`` resolves per request rather than the
    application object: the server captures the application before any
    module is imported, so replacing that would change nothing, while
    the name inside ``Application.__call__`` is looked up on every
    request. Verified against this Odoo rather than assumed.

    Does nothing unless the header and some public network are
    configured, so an installation that is not behind a CDN is
    untouched. A network that spans private addresses is pointed out:
    nothing private is believed from it, but the list is unlikely to be
    what was meant. The proxy's name is optional, and asked for here so
    that one that does not resolve is in the log from the start; see
    :class:`OwnProxy`.

    :param config: the Odoo configuration
    :param http_module: the ``odoo.http`` module
    :return: whether the wrapper was installed
    :rtype: bool
    """
    header = (config.get(HEADER_OPTION) or "").strip()
    networks = cdn_networks(config.get(TRUSTED_OPTION))
    if not (header and networks):
        return False
    if getattr(http_module.ProxyFix, "_incubacloud_real_ip", False):
        return False
    for network in networks:
        if any(network.version == own.version and network.overlaps(own)
               for own in OWN_PROXY_NETWORKS):
            _logger.warning(
                "[real-ip] %s overlaps private addresses: none of them is "
                "taken for the CDN's, but check the list",
                network,
            )
    proxy = OwnProxy(config.get(PROXY_OPTION))
    proxy.look_up()

    original = http_module.ProxyFix

    def proxy_fix_reading_the_real_client(app):
        """Stand in for ``ProxyFix``, correcting the chain first."""
        inner = original(app)

        def apply(environ, start_response):
            rewrite(environ, header, networks, proxy)
            return inner(environ, start_response)

        return apply

    proxy_fix_reading_the_real_client._incubacloud_real_ip = True
    # What was replaced, kept on the replacement. Installing happens once
    # per process, at import, so anything that needs the original back —
    # a test asserting on the uncorrected behaviour, above all — has no
    # other way to reach it, and guessing at Odoo's own definition would
    # drift the day Odoo changes it.
    proxy_fix_reading_the_real_client._incubacloud_wrapped = original
    http_module.ProxyFix = proxy_fix_reading_the_real_client
    _logger.info(
        "[real-ip] honouring %s from %d trusted network(s), through %s",
        header, len(networks), proxy,
    )
    return True
