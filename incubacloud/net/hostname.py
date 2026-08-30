r"""Validation for hostnames that end up inside generated configuration.

``cloud.host.wildcard_domain`` is the one hostname on a host record that
nobody types twice: it is written once at provisioning time and then read
back by everything the platform generates for that machine — the Traefik
router rule, the certificate it asks for, the subdomain of every instance
that lands there. Until this module existed the field was ``required=True``
and nothing else: no shape, no length, no character set.

Two separate things went wrong with that.

The first needs no attacker. ``full_setup_executor._build_inverseproxy``
splices the value into a Traefik rule delimited by backticks, and it did so
through the *replacement* argument of ``re.sub`` — where Python interprets
backslashes. A domain carrying ``\d`` raised ``re.error: bad escape`` and
killed the setup job halfway through; one carrying a backtick closed the
rule early and turned the dashboard router into a catch-all. An operator
typo reaches both.

The second is why this lives in ``net`` rather than in a model. Ordering an
on-demand VPS has two doors. The tenant's own controller validated hard —
shape, an internal-address blocklist, a platform-reserved list — but the
door that actually decides is the manager's ``/saas/vps/request``, reached
with a tenant token that the tenant's own administrator can read straight
out of their database. That endpoint checked the length and nothing else,
and the tenant executor then takes whatever the manager echoes back as
authoritative. So the strict check was never the one that mattered. Both
doors call this module now, and the constraint underneath means a value
that somehow skips both still cannot land on a host.

The split of responsibilities is deliberate:

* :func:`validate_wildcard_domain` with no flags checks **shape only** —
  RFC 1123, lengths, ASCII. That is what ``cloud.host`` enforces, because
  that table also holds hosts an operator named by hand, and calling one
  ``h.local`` on an internal network is legitimate.
* ``check_internal`` and ``reserved`` add the policy that only means
  something where the value is "a public DNS name a tenant chose". That is
  the two BYOD doors, and nowhere else.

Comparison is case-insensitive and the stored value is **not** rewritten.
DNS is case-insensitive, production already holds a host recorded as
``Tenants1.incubacloud.io``, and this codebase deliberately refuses to
silently lower-case what an operator typed: a canonical hostname that
differs from what they entered is its own kind of surprise.
"""
import re

#: IANA cap on a fully-qualified domain name.
MAX_HOSTNAME_LEN = 253

# RFC 1123 label: alphanumeric at both ends, hyphens allowed inside, 1-63
# characters. Anchored, so an empty label — what ``..`` or a trailing dot
# produces after the split — fails here rather than needing its own check.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Names that must never be reachable as a public wildcard: they point DNS
# at internal infrastructure (CWE-918).
BLOCKED_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "metadata.google.internal",
    "metadata.azure.com",
    "metadata.tencentyun.com",
})
BLOCKED_SUFFIXES = (
    ".local",
    ".internal",
    ".localdomain",
    ".consul",
    ".k8s",
    ".cluster.local",
)
BLOCKED_PREFIXES = (
    "10.",
    "127.",
    "169.254.",
    "192.168.",
) + tuple(f"172.{octet}." for octet in range(16, 32))


class InvalidHostname(ValueError):
    """A hostname that must not be persisted or spliced into config.

    ``code`` names the rule that rejected the value so a caller can phrase
    its own user-facing message without parsing the text: ``empty``,
    ``non_ascii``, ``too_long``, ``shape``, ``internal`` or ``reserved``.

    :param code: identifier of the rule that failed.
    :param message: plain-English reason, for logs and for callers that
        have nothing better to show.
    """

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def validate_wildcard_domain(value, *, check_internal=False, reserved=()):
    """Return *value* normalised, or raise :class:`InvalidHostname`.

    :param value: candidate domain, with or without a leading ``*.``.
    :param check_internal: also refuse names that are, or sit under,
        internal infrastructure. Turn it on at the doors where the value is
        a public DNS name a tenant chose; leave it off for the model
        constraint, which also guards operator-named hosts.
    :param reserved: domains the platform keeps for itself. A candidate
        equal to one of them, or underneath one, is refused.
    :returns: the domain stripped and lower-cased, ``*.`` prefix intact.
    :raises InvalidHostname: carrying the ``code`` of the rule that failed.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidHostname("empty", "A wildcard domain is required.")
    # Before lower-casing, not after: ``str.lower`` on some alphabets maps
    # to characters that then sail through an ASCII-only regex.
    if not candidate.isascii():
        raise InvalidHostname(
            "non_ascii",
            "A wildcard domain must be ASCII; punycode an IDN first.",
        )
    candidate = candidate.lower()
    bare = candidate.removeprefix("*.")
    if len(bare) > MAX_HOSTNAME_LEN:
        raise InvalidHostname(
            "too_long",
            f"A wildcard domain must be at most {MAX_HOSTNAME_LEN} "
            "characters.",
        )
    labels = bare.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        raise InvalidHostname(
            "shape",
            "A wildcard domain must be a valid DNS hostname "
            "(e.g. app.example.com).",
        )
    if check_internal and (
        bare in BLOCKED_HOSTS
        or bare.endswith(BLOCKED_SUFFIXES)
        or bare.startswith(BLOCKED_PREFIXES)
    ):
        raise InvalidHostname(
            "internal", "Internal addresses are not allowed.",
        )
    for entry in reserved:
        owned = (entry or "").strip().lower().removeprefix("*.")
        if owned and (bare == owned or bare.endswith("." + owned)):
            raise InvalidHostname(
                "reserved", "This domain is reserved by the platform.",
            )
    return candidate
