"""A real certificate and key for tests that need a host to hold one.

Generated per call rather than pasted in as a constant: a fixture that
looks like a private key is one a secret scanner has to be told to
ignore, and one a reader has to decide about. This way there is nothing
to ignore and nothing to decide.

The pair is only ever valid to itself — self-signed, by an issuer that
exists for the length of the call.
"""
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

DEFAULT_HOSTNAMES = ("example.test", "*.example.test")


def make_pair(hostnames=DEFAULT_HOSTNAMES, issuer="Test CA", with_san=True):
    """Return a matching ``(certificate PEM, private key PEM)``.

    :param hostnames: names for the subject alternative name extension;
        the first is also the subject common name.
    :param str issuer: common name to sign with, so a test can tell two
        certificates apart by where they claim to come from.
    :param bool with_san: when false the extension is left out entirely
        and the names survive only as the common name — the shape that
        proves coverage is read from the extension and nowhere else.
    :rtype: tuple
    """
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0]),
        ]))
        .issuer_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, issuer),
        ]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=5475))
    )
    if with_san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(name) for name in hostnames],
            ),
            critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
    )
