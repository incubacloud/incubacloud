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


async def presented_host_key_async(address, port):
    """Return the key the server at *address* presents, without logging in.

    :param str address: the address to dial (already checked by the
        caller against its network policy)
    :param int port: the SSH port
    :return: the ``asyncssh`` key, or None when none was presented
    :raise asyncssh.Error, OSError: on a failed connection
    :raise asyncio.TimeoutError: past :data:`TOTAL_TIMEOUT`
    """
    options = asyncssh.SSHClientConnectionOptions(
        connect_timeout=CONNECT_TIMEOUT,
    )
    return await asyncio.wait_for(
        asyncssh.get_server_host_key(address, int(port or 22), options=options),
        TOTAL_TIMEOUT,
    )


def presented_host_key(address, port):
    """Synchronous :func:`presented_host_key_async`, on a loop of its own.

    Only for a caller outside an event loop (a route, a cron).
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(presented_host_key_async(address, port))
    finally:
        loop.close()
