"""Client identity behind a chain of proxies.

Everything that keys on "who is asking" — a rate limit, an allowlist, an
audit line — is only as good as the address it reads. Behind a proxy
that address is the proxy's, and the real one arrives in
``X-Forwarded-For``: a header any client can write. So the value is
trustworthy only when the hop that appended it is known to be trusted,
and untrustworthy the moment it is not.

The rule this module implements is the standard one: walk the forwarded
chain from the right, discarding entries that belong to a proxy we
operate, and stop at the first address that does not. That address is
the closest hop we have no reason to believe — the client, if our
proxies are the only ones in front, and an attacker-controlled value
only in the portion of the chain we already refuse to read.

**With no trusted ranges configured, every function here returns the
connecting address unchanged.** A deployment that has not declared its
proxies keeps the behaviour it had before this module existed, which is
also the safe one: over-restrictive (everyone behind the proxy shares a
bucket) rather than spoofable.
"""
import ipaddress


def _entries(raw):
    """Split a stored field into its non-empty entries.

    Newlines and commas both separate, so a value pasted from a
    provider's documentation works whichever way it was written.

    :param raw: the stored field value, or None
    :rtype: list
    """
    if not raw:
        return []
    return [
        chunk.strip()
        for chunk in str(raw).replace(",", "\n").splitlines()
        if chunk.strip()
    ]


def parse_ranges(raw):
    """Return the CIDR strings in a newline- or comma-separated field.

    Entries that are not networks are skipped rather than raised on:
    this runs on the request path, where an exception would turn a
    rate-limit check into a 500. Writing such a value is refused at the
    model instead — see :func:`invalid_ranges` — so what reaches here
    has already been checked.

    :param raw: the stored field value, or None
    :return: normalised CIDR strings
    :rtype: list
    """
    ranges = []
    for entry in _entries(raw):
        try:
            ranges.append(str(ipaddress.ip_network(entry, strict=False)))
        except ValueError:
            continue
    return ranges


def invalid_ranges(raw):
    """Return the entries of *raw* that are not networks.

    The write-side counterpart of :func:`parse_ranges`. A range dropped
    for a typo is exactly the failure this whole mechanism exists to
    avoid — narrower trust than intended, with nothing said about it —
    so the value is refused at the point where somebody can still fix
    it rather than silently trimmed later.

    :param raw: the value about to be stored
    :return: the unusable entries, in the order they were written
    :rtype: list
    """
    bad = []
    for entry in _entries(raw):
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            bad.append(entry)
    return bad


def networks(ranges):
    """Return the parsed networks for *ranges*, skipping unusable ones.

    :param ranges: CIDR strings
    :return: ``ip_network`` objects
    :rtype: list
    """
    parsed = []
    for entry in ranges or []:
        try:
            parsed.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError:
            continue
    return parsed


def is_trusted(address, ranges):
    """Return whether *address* falls inside any of *ranges*.

    :param address: an IP address as a string
    :param ranges: CIDR strings
    :rtype: bool
    """
    try:
        ip = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    return any(ip in network for network in networks(ranges))


def client_ip(remote_addr, forwarded_for, ranges):
    """Return the closest address in the chain we have no reason to trust.

    :param remote_addr: the address the socket was opened from
    :param forwarded_for: the forwarded chain, left to right, as a list
        of addresses or a raw ``X-Forwarded-For`` string
    :param ranges: CIDR strings naming the proxies we operate
    :return: the client address, or ``remote_addr`` when nothing in the
        chain can be believed
    :rtype: str
    """
    remote = (str(remote_addr).strip() if remote_addr else "") or ""
    trusted = networks(ranges)
    if not trusted:
        # Nothing declared: the header is unsigned data from an unknown
        # network. Reading it would let any client choose its own
        # identity, so the connecting address is the only honest answer.
        return remote
    if isinstance(forwarded_for, str):
        chain = [part.strip() for part in forwarded_for.split(",")]
    else:
        chain = [str(part).strip() for part in (forwarded_for or [])]
    chain = [entry for entry in chain if entry]

    def _trusted(value):
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(ip in network for network in trusted)

    if remote and not _trusted(remote):
        # The request did not arrive through one of our proxies, so
        # whatever it carries in the header was written by the caller.
        return remote
    for entry in reversed(chain):
        if not _trusted(entry):
            return entry
    # Every hop is ours. Either the chain is empty (a request from the
    # proxy itself, such as a health check) or an internal caller.
    return remote
