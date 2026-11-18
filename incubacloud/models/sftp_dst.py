"""What an ``sftp://`` backup destination is, read one way everywhere.

A duplicity DST for SFTP reaches four readers: the template (which picks
the image), duplicity on the host, the purge program and the panel. Read
four ways, a frozen path written by hand could send the copy to one
server and the purge to another. So there is one reading, duplicity's:
``urlparse`` and drop a single leading slash. ``sftp://u@h/backups`` is
``backups`` under the user's home, ``sftp://u@h//srv/backups`` is the
absolute ``/srv/backups``, and ``sftp://u@h`` is the home itself.

And one rule of identity: the user, the server and the port always come
from the backup destination, never from a DST. A DST only says *where*
on that server. A DST that names another server or another user is
refused (:func:`check_dst`), so a path typed into ``custom_backup_dst``
cannot point the panel, or a key it holds, somewhere else.

The purge program carries a copy of :func:`parse` (it runs inside the
backup container, with no access to this module); both are tested
against the same table of cases (``tests/data/sftp_dst_cases.json``).
"""

import re
from urllib.parse import urlparse

SCHEME = "sftp"

#: A user name as SFTP providers hand them out: ``u123456-sub1``, or a
#: compound ``account.container.user``.
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")

#: One path segment. Nothing a shell, a URL or YAML would read twice.
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


#: Where an instance keeps the files its ``backup`` service logs in with,
#: relative to the instance directory. Mounted read-only as ``/root/.ssh``
#: over the ``backup_cache`` volume, so no key is ever written into it.
SSH_DIR = ".docker/backup-ssh"
SSH_MOUNT = f"./{SSH_DIR}:/root/.ssh:ro"
SSH_KEY_FILE = f"{SSH_DIR}/id_ed25519"
SSH_KNOWN_HOSTS_FILE = f"{SSH_DIR}/known_hosts"
SSH_CONFIG_FILE = f"{SSH_DIR}/config"

#: What ``~/.ssh/config`` says in the container. ``Host *``: the
#: container talks to its destination only, so a frozen DST keeps the
#: same identity. duplicity's paramiko reads the identity from here and
#: ``~/.ssh/known_hosts`` by its default path; it does not read
#: ``StrictHostKeyChecking`` nor ``UserKnownHostsFile`` (it asks on the
#: standard input about an unknown server, which the commands close).
#: Those two are for an OpenSSH client run by hand in the container.
SSH_CONFIG = (
    "Host *\n"
    "  IdentityFile /root/.ssh/id_ed25519\n"
    "  IdentitiesOnly yes\n"
    "  UserKnownHostsFile /root/.ssh/known_hosts\n"
    "  StrictHostKeyChecking yes\n"
)

#: ``~/.ssh/config`` of a destination that logs in with a password: no
#: identity. duplicity's paramiko then offers no key and logs in with
#: ``BACKEND_PASSWORD`` (``Backend.get_password`` of duplicity 3.0.5.1),
#: which ``backup.env`` carries; the server key is checked as with a key.
SSH_CONFIG_PASSWORD = (
    "Host *\n"
    "  UserKnownHostsFile /root/.ssh/known_hosts\n"
    "  StrictHostKeyChecking yes\n"
)


class DstError(ValueError):
    """A DST that is not an ``sftp://`` URL this panel can use."""


class DstMismatch(DstError):
    """A DST that names another server, port or user than its destination."""


def check_path(path):
    """Return *path* normalised, or raise ``DstError``.

    A leading ``/`` makes it absolute; a trailing one is dropped. Every
    segment must match :data:`SEGMENT_RE`, and none may be empty, ``.``
    or ``..``.

    :param str path: the path as typed (``backups``, ``/srv/backups``)
    :return: the path without a trailing slash; ``""`` for the home
    :rtype: str
    :raise DstError: on a segment that is not allowed
    """
    path = (path or "").strip()
    absolute = path.startswith("/")
    body = path.strip("/")
    if not body:
        if absolute:
            raise DstError("the root directory cannot hold backups")
        return ""
    for segment in body.split("/"):
        if not segment or segment in (".", "..") or not SEGMENT_RE.match(segment):
            raise DstError(f"path segment not allowed: {segment!r}")
    return ("/" if absolute else "") + body


def build(user, host, port, path):
    """Return the duplicity DST of a destination.

    :param str user: the SFTP user
    :param str host: the server's name or IPv4 address
    :param int port: the SSH port
    :param str path: relative to the user's home, or absolute with ``/``
    :return: ``sftp://user@host[:port]`` plus ``/path`` (``//path`` when
        absolute); without a trailing slash, so that appending
        ``/{project}/{instance}`` keeps a relative path relative
    :rtype: str
    """
    port = int(port or 22)
    netloc = f"{user}@{host}" + (f":{port}" if port != 22 else "")
    path = check_path(path)
    return f"{SCHEME}://{netloc}" + (f"/{path}" if path else "")


def parse(dst):
    """Read an ``sftp://`` DST as duplicity does.

    :param str dst: ``sftp://user@host[:port][/path]``
    :return: ``(user, host, port, path)``, *path* relative to the home or
        absolute with a leading ``/``, ``""`` for the home
    :rtype: tuple
    :raise DstError: when *dst* is not such a URL
    """
    parsed = urlparse(dst or "")
    if parsed.scheme != SCHEME:
        raise DstError(f"not an {SCHEME}:// destination")
    if "?" in dst or "#" in dst:
        # urlparse drops them from the path: what follows would be read
        # by one reader and not by another.
        raise DstError("a query or a fragment does not belong in the destination")
    if parsed.password is not None:
        raise DstError("a password does not belong in the destination")
    user = parsed.username or ""
    host = parsed.hostname or ""
    if not user or not host:
        raise DstError("the destination names no user or no server")
    if not USER_RE.match(user):
        raise DstError(f"user name not allowed: {user!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DstError("the port is not a number from 1 to 65535") from exc
    if port == 0:
        raise DstError("the port is not a number from 1 to 65535")
    port = port or 22
    raw = parsed.path.removeprefix("/")
    return user, host, port, check_path(raw)


def check_dst(user, host, port, dst):
    """Return the path of *dst*, refusing one off its destination.

    :param str user: the destination's user
    :param str host: the destination's server
    :param int port: the destination's port
    :param str dst: the DST to check (an instance's, a frozen one)
    :return: the path of *dst* (:func:`parse`)
    :rtype: str
    :raise DstMismatch: when *dst* names another user, server or port
    :raise DstError: when *dst* is not an ``sftp://`` URL
    """
    d_user, d_host, d_port, path = parse(dst)
    if (d_user, d_host.lower(), d_port) != (
        user, (host or "").lower(), int(port or 22),
    ):
        raise DstMismatch(
            f"{dst} is not on this destination's server "
            f"({user}@{host}:{int(port or 22)})"
        )
    return path


def instance_path(path):
    """Return whether *path* names one instance's copy.

    An instance's DST ends in ``{project}/{instance}``: two segments at
    least. Anything shorter is a destination shared by others, which a
    purge or a measure must never take as one instance's.

    :param str path: a path from :func:`parse`
    :rtype: bool
    """
    return len([s for s in path.split("/") if s]) >= 2
