"""Which names a certificate actually covers.

A router that asks a CA for a certificate it can never obtain fails in
the quietest way there is: the proxy keeps serving the certificate it
already has until that one expires, and only then starts answering
handshakes with a throwaway. Nothing logs a decision, because no
decision was ever made.

Avoiding that means one question has to be answerable before the
configuration is written: does the certificate this host already holds
cover the name this router serves? The certificate itself is the only
honest answer. Deriving it from a base domain would be a guess that
happens to be right for the certificate we issue today and wrong for
the next one — a purchased wildcard, an internal CA, a certificate
covering two zones.

So these functions read the certificate. They are deliberately free of
Odoo: the matching rules below are the interoperable ones every TLS
client implements, and they are worth being able to test on their own.
"""
import re

from cryptography import x509

# A wildcard is accepted only as an entire leftmost label. RFC 6125
# permits a partial one (``f*.example.com``) but tells callers not to
# rely on it, and the major TLS clients refuse it outright — so a
# certificate carrying one would match here and be rejected by the
# browser it was meant for. Treating it as a literal is what keeps this
# function's answer and the visitor's experience the same answer.
_WILDCARD_LABEL = "*"

_TRAILING_DOT = re.compile(r"\.+$")


def normalise(name):
    """Return *name* in the form these comparisons are made in.

    Lowercased and stripped of the trailing root dot, so ``Example.COM``
    and ``example.com.`` are the same name — which they are.

    :param str name: a hostname, from a certificate or a router rule
    :rtype: str
    """
    return _TRAILING_DOT.sub("", (name or "").strip().lower())


def certificate_names(cert_pem):
    """Return the DNS names a PEM certificate presents itself for.

    Read from the subject alternative name extension only. The legacy
    common name is deliberately ignored: TLS clients stopped honouring
    it years ago, so a certificate whose coverage lived only there would
    be accepted here and rejected by every browser.

    :param str cert_pem: PEM-encoded certificate, possibly empty
    :return: lowercased DNS names, empty when there is nothing to read
    :rtype: list
    """
    pem = (cert_pem or "").strip()
    if not pem:
        return []
    try:
        loaded = x509.load_pem_x509_certificate(pem.encode())
    except (ValueError, TypeError):
        return []
    try:
        names = loaded.extensions.get_extension_for_class(
            x509.SubjectAlternativeName,
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return []
    return [normalise(name) for name in names if name]


def name_matches(hostname, certificate_name):
    """Return whether *certificate_name* stands for *hostname*.

    Implements the wildcard rule TLS clients implement: a leading
    ``*.`` matches exactly one label, so ``*.example.com`` covers
    ``a.example.com`` but neither ``example.com`` nor
    ``a.b.example.com``. Anything else is an exact comparison.

    :param str hostname: the name a router serves
    :param str certificate_name: one name out of a certificate
    :rtype: bool
    """
    host = normalise(hostname)
    cert = normalise(certificate_name)
    if not host or not cert:
        return False
    if not cert.startswith(_WILDCARD_LABEL + "."):
        return cert == host
    suffix = cert[len(_WILDCARD_LABEL) + 1:]
    if not suffix:
        return False
    head, _, tail = host.partition(".")
    return bool(head) and tail == suffix


def covers(hostname, names):
    """Return whether any of *names* stands for *hostname*.

    :param str hostname: the name a router serves
    :param names: DNS names out of a certificate
    :rtype: bool
    """
    return any(name_matches(hostname, name) for name in names or [])
