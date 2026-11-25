"""Reading and filing an SSH server's key: shared by hosts and SFTP destinations.

What a server presents is read with the key exchange alone: no user name,
password or client key is sent, so asking a server that is not the
trusted one gives it nothing. The line is filed under the name the
consumer will look it up by (:func:`known_hosts_prefix`), and the
fingerprint is the ``SHA256:…`` form ``ssh-keygen -lf`` prints, so a
person can hold it next to what the server's provider shows.
"""

import asyncio
import base64
import hashlib

import asyncssh

#: Seconds to open the TCP connection, and for the whole exchange.
CONNECT_TIMEOUT = 10
TOTAL_TIMEOUT = 30

#: Server key types a trusted line may hold: those duplicity's paramiko 4
#: reads in ``known_hosts`` (ed25519, ECDSA and RSA).
ACCEPTED_KEY_TYPES = frozenset({
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
})

#: The order an SFTP destination's key is read in: the one duplicity's
#: paramiko 4.0.0 asks a server in (``Transport._preferred_keys``), as the
#: backup image ``ghcr.io/tecnativa/docker-duplicity-postgres-s3`` ships it
#: (checked on 25-nov-2026). paramiko puts the type of the trusted line
#: first and compares the key the server answers with that line; a server
#: that does not offer that type answers with the next one in this order,
#: and the backups fail with "Host key for server … does not match". An
#: ``ssh-rsa`` line fails so on every OpenSSH 8.8 or later that also has
#: an Ed25519 key, since it no longer offers ``ssh-rsa`` (SHA-1): paramiko
#: gets the Ed25519 key. Reading in this order files the key paramiko gets
#: when it trusts nothing yet. asyncssh's own order puts RSA first, so the
#: hosts, whose keys OpenSSH and asyncssh check, keep it
#: (``cloud.host._presented_host_key_entry``): moving them would make every
#: host with an RSA key trusted look as if its key had changed.
PARAMIKO_HOST_KEY_ORDER = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
)

#: RSA alone: whether a server still presents the RSA key a destination
#: trusts, when it answers the order above with another type.
RSA_HOST_KEY_ORDER = ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")


def paramiko_order(trusted_type=""):
    """Return the order paramiko asks in when it trusts a *trusted_type* line.

    paramiko moves the trusted line's type to the front of
    :data:`PARAMIKO_HOST_KEY_ORDER` (``client.py``, ``keytype =
    our_server_keys.keys()[0]``), and compares what the server answers
    with that line. Read in this order, the key is the one the backups get.

    :param str trusted_type: the trusted line's type (:func:`key_type`),
        ``''`` when none is trusted
    :rtype: tuple
    """
    if trusted_type not in PARAMIKO_HOST_KEY_ORDER:
        return PARAMIKO_HOST_KEY_ORDER
    return (trusted_type,) + tuple(
        alg for alg in PARAMIKO_HOST_KEY_ORDER if alg != trusted_type
    )


def known_hosts_prefix(host, port):
    """Return the ``known_hosts`` host pattern for a (host, port) endpoint.

    OpenSSH and paramiko file a non-default port under ``[host]:port``;
    the bare ``host`` form matches port 22 and nothing else. asyncssh is
    laxer and accepts the bare form on any port, which is precisely how a
    stale label stays invisible until an OpenSSH-based consumer reaches
    the same host and reports it as unknown.

    :param str host: the name or address, as the consumer will look it up
    :param int port: the SSH port
    :rtype: str
    """
    port = int(port or 22)
    return f"[{host}]:{port}" if port != 22 else str(host)


def known_hosts_line(host, port, server_key):
    """Return the ``known_hosts`` line of *server_key* at an endpoint.

    :param str host: the label (:func:`known_hosts_prefix`)
    :param int port: the SSH port
    :param server_key: the ``asyncssh`` key the server presented
    :rtype: str
    """
    key_data = server_key.export_public_key("openssh").decode().strip()
    return f"{known_hosts_prefix(host, port)} {key_data}"


def key_type(entry):
    """Return the key type of the first line of *entry* (``ssh-ed25519``…).

    :param str entry: a ``known_hosts`` line
    :return: the type, or ``''`` without one
    :rtype: str
    """
    for line in (entry or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.lstrip().startswith("#"):
            return parts[1]
    return ""


def fingerprint(entry):
    """Return the ``SHA256:…`` fingerprint of a ``known_hosts`` line.

    A ``known_hosts`` line is ``<pattern> <keytype> <base64> [comment]``,
    so the key material is the *third* field.

    :param str entry: one or more ``known_hosts`` lines
    :returns: the fingerprint of the first decodable line, or ``''`` when
        none carries a decodable key (blank, comment-only, garbage)
    :rtype: str
    """
    for line in (entry or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or line.lstrip().startswith("#"):
            continue
        try:
            digest = hashlib.sha256(base64.b64decode(parts[2])).digest()
        except Exception:  # noqa: BLE001 — not a key: try the next line
            continue
        value = base64.b64encode(digest).rstrip(b"=").decode("ascii")
        return f"SHA256:{value}"
    return ""


async def presented_host_key_async(address, port, server_host_key_algs=()):
    """Return the key the server at *address* presents, without logging in.

    The key types go to ``get_server_host_key`` as its own keyword: given
    inside ``options`` they are overridden by that keyword's default. An
    empty tuple is that default; an empty list would select no type.

    No client key and no agent are loaded (``client_keys=None``,
    ``agent_path=None``): none is sent, and asyncssh would otherwise read
    ``~/.ssh`` first, where a key it cannot import (the empty ``id_rsa``
    a Doodba project ships) stops the reading with ``KeyImportError``.

    :param str address: the address to dial (already checked by the
        caller against its network policy)
    :param int port: the SSH port
    :param server_host_key_algs: the key types to ask for, in order
        (:data:`PARAMIKO_HOST_KEY_ORDER`); empty for asyncssh's own
    :return: the ``asyncssh`` key, or None when none was presented
    :raise asyncssh.Error, OSError: on a failed connection, or a server
        that offers none of the types asked
    :raise ValueError: on options asyncssh refuses
    :raise asyncio.TimeoutError: past :data:`TOTAL_TIMEOUT`
    """
    options = asyncssh.SSHClientConnectionOptions(
        connect_timeout=CONNECT_TIMEOUT, client_keys=None, agent_path=None,
    )
    return await asyncio.wait_for(
        asyncssh.get_server_host_key(
            address, int(port or 22),
            server_host_key_algs=tuple(server_host_key_algs),
            options=options,
        ),
        TOTAL_TIMEOUT,
    )


def presented_host_key(address, port, server_host_key_algs=()):
    """Synchronous :func:`presented_host_key_async`, on a loop of its own.

    Only for a caller outside an event loop (a route, a cron).
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(presented_host_key_async(
            address, port, server_host_key_algs=server_host_key_algs,
        ))
    finally:
        loop.close()
