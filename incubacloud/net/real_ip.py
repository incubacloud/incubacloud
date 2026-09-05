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
that assertion into the one Odoo already reads, and only when it comes
from an address we have been told to believe — an unproven header is
written by whoever is calling, so honouring one from an untrusted peer
would let any visitor claim to be any address.

Deliberately free of any particular CDN: the header name and the
addresses to believe are given by the caller.
"""
import ipaddress
import logging

_logger = logging.getLogger(__name__)

#: Config keys read from the Odoo configuration file. Read from there
#: rather than from a record because this runs before any registry
#: exists — it is installed while the module is imported.
HEADER_OPTION = "x_real_ip_header"
TRUSTED_OPTION = "x_real_ip_trusted"


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


def is_trusted(address, networks):
    """Return whether *address* falls inside any of *networks*.

    :param str address: the address a connection was opened from
    :param networks: networks whose forwarded headers are believed
    :rtype: bool
    """
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    return any(ip in network for network in networks)


def asserted_client(environ, header, networks):
    """Return the visitor address a trusted proxy claims, or ``None``.

    ``None`` covers every case where nothing should change: no header
    configured, no networks to believe, a connection from somewhere
    else, a header that is absent or holds something that is not an
    address.

    Only the first entry is read. The header is the proxy's own
    assertion about its client; anything after a comma was appended by
    somebody further out and is not the proxy speaking.

    :param dict environ: the WSGI environment
    :param str header: header the proxy states the visitor in
    :param networks: networks whose assertion is believed
    :rtype: str | None
    """
    if not (header and networks):
        return None
    if not is_trusted(environ.get("REMOTE_ADDR"), networks):
        return None
    claimed = (environ.get(environ_key(header)) or "").split(",")[0].strip()
    try:
        ipaddress.ip_address(claimed)
    except ValueError:
        return None
    return claimed


def rewrite(environ, header, networks):
    """Make the visitor the last entry of the forwarded chain.

    Rewritten rather than appended: the chain becomes what it would
    have been with the proxy as the only hop, which is what the entry
    Odoo reads is supposed to mean. Keeping the rest would leave the
    edge's address in a position that claims to be a client.

    :return: the address written, or ``None`` when nothing changed
    :rtype: str | None
    """
    client = asserted_client(environ, header, networks)
    if client:
        environ["HTTP_X_FORWARDED_FOR"] = client
    return client


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

    Does nothing unless both options are configured, so an installation
    that is not behind a CDN is untouched.

    :param config: the Odoo configuration
    :param http_module: the ``odoo.http`` module
    :return: whether the wrapper was installed
    :rtype: bool
    """
    header = (config.get(HEADER_OPTION) or "").strip()
    networks = parse_networks(config.get(TRUSTED_OPTION))
    if not (header and networks):
        return False
    if getattr(http_module.ProxyFix, "_incubacloud_real_ip", False):
        return False

    original = http_module.ProxyFix

    def proxy_fix_reading_the_real_client(app):
        """Stand in for ``ProxyFix``, correcting the chain first."""
        inner = original(app)

        def apply(environ, start_response):
            rewrite(environ, header, networks)
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
        "[real-ip] honouring %s from %d trusted network(s)",
        header, len(networks),
    )
    return True
