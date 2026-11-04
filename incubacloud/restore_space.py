"""How much of a host's disk a restore may take, in one place.

A restore lands its archive on the host, in ``/tmp``, and unpacks it into
the instance's database and filestore: all of it on the disk the host's
other instances write their databases, logs and backups to. An archive
larger than what is free, or a server that never stops sending, filled
that disk and stopped every instance on it. So a restore measures what is
free first, on the host, and takes no more than this leaves:

* :data:`RESERVE_BYTES` stay free whatever the restore needs: what the
  other instances keep writing while it runs.
* Beyond the archive, :data:`UNPACK_FACTOR` times its size is kept for
  unpacking and loading it (the dump and the filestore taken out of it,
  then the database they become). A floor, not a promise: a dump that
  compressed tenfold needs more, and fails while loading, as before, with
  the reserve still free.

A download from a link is given the largest archive these leave
(:func:`largest_download`); an archive already on the host — uploaded
through a door, or copied to the agreed path — is restored only when what
is free covers what it needs (:func:`room_needed`), and a check of what
came through a door says so before anyone asks for the restore.
"""
import shlex

#: 1 GiB, the unit every message speaks in.
GIB = 1024 ** 3

#: What a restore always leaves free on its host's disk.
RESERVE_BYTES = 5 * GIB

#: Room kept beyond the archive to unpack and load it, as a multiple of
#: its size.
UNPACK_FACTOR = 2


def free_space_command(path):
    """Return the shell that prints the bytes free where *path* lives.

    :param str path: a directory on the host
    :rtype: str
    """
    return f"df -B1 --output=avail -- {shlex.quote(path)} | tail -n 1"


def parse_free(stdout):
    """Return the bytes ``free_space_command`` printed, or None.

    :param str stdout: what it printed
    :rtype: int | None
    """
    text = (stdout or "").strip().splitlines()
    value = text[-1].strip() if text else ""
    return int(value) if value.isdigit() else None


def largest_download(free):
    """Return the largest archive a download may write with *free* bytes free.

    What is left above the reserve, shared between the archive and its
    unpacking: ``0`` when nothing is.

    :param int free: bytes free on the disk the archive lands on
    :rtype: int
    """
    return max(0, (free - RESERVE_BYTES) // (1 + UNPACK_FACTOR))


def room_needed(archive_bytes, copies=0):
    """Return the bytes that must be free to restore an archive on the host.

    :param int archive_bytes: the archive's size
    :param int copies: further copies of it the restore makes first (an
        upload through a door is copied out of the door's directory)
    :rtype: int
    """
    return RESERVE_BYTES + int(archive_bytes) * (copies + UNPACK_FACTOR)


def in_gib(size):
    """Return *size* in GiB, as the messages say it.

    :param int size: bytes
    :rtype: str
    """
    return f"{size / GIB:.1f} GiB"


def no_room_message(free, needed, what):
    """Return why an archive cannot be restored with *free* bytes free.

    :param int free: bytes free on the host
    :param int needed: bytes the restore needs free
    :param str what: the archive, as the message names it
    :rtype: str
    """
    return (
        f"Not enough free disk on the server for {what}: "
        f"{in_gib(free)} free, {in_gib(needed)} needed (the archive, "
        f"{UNPACK_FACTOR} times its size to unpack it, and "
        f"{in_gib(RESERVE_BYTES)} the server's other instances keep). "
        "Nothing was restored: free space on the server, or restore a "
        "smaller archive."
    )
