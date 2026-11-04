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

The archive lands in ``/tmp``; it is unpacked and loaded inside the
instance's containers, on the disk Docker keeps them on
(``DockerRootDir``). The two may be one disk, or two: ``/tmp`` a small
tmpfs in memory and Docker's a large disk, or the other way round. So a
restore measures both (:func:`disks_command`, :class:`Disks`): on one
disk it needs all of the above there; on two, room for the archive and
its copies in ``/tmp``, and the reserve and the unpacking on Docker's
(:func:`largest_archive`, :func:`room_refusal`).
"""
import shlex
from collections import namedtuple

#: 1 GiB, the unit every message speaks in.
GIB = 1024 ** 3

#: What a restore always leaves free on its host's disk.
RESERVE_BYTES = 5 * GIB

#: Room kept beyond the archive to unpack and load it, as a multiple of
#: its size.
UNPACK_FACTOR = 2


def parse_free(stdout):
    """Return the byte count a command printed last (``stat -c %s``), or None.

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


class Disks(namedtuple("Disks", "job docker shared")):
    """The two disks a restore writes on, as the host measured them.

    :ivar int job: bytes free on the disk of the job's directory (``/tmp``)
    :ivar int docker: bytes free on the disk Docker keeps the instances on
    :ivar bool shared: whether the two are one disk
    """

    __slots__ = ()

    def free(self):
        """Return the bytes free of one disk shared: the lesser reading.

        :rtype: int
        """
        return min(self.job, self.docker)


def disks_command(path):
    """Return the shell that prints the device and bytes free of two disks.

    One line ``<device> <bytes free>`` for the disk *path* lives on, then
    one for the disk Docker keeps its data on (``DockerRootDir``). Fails,
    or prints less, when either cannot be read: :func:`parse_disks` then
    answers None, and nothing is restored.

    :param str path: a directory on the host (the job's, or ``/tmp``)
    :rtype: str
    """
    measure = (
        'm() { d=$(stat -c %d -- "$1") && a=$(df -B1 --output=avail -- "$1"'
        ' | tail -n 1) && printf "%s %s\\n" "$d" "$a"; }'
    )
    return (
        f"{measure}; m {shlex.quote(path)}"
        " && root=$(docker info -f '{{.DockerRootDir}}') && [ -n \"$root\" ]"
        ' && m "$root"'
    )


def parse_disks(stdout):
    """Return what :func:`disks_command` printed as :class:`Disks`, or None.

    :param str stdout: what it printed
    :rtype: Disks | None
    """
    rows = []
    for line in (stdout or "").strip().splitlines()[-2:]:
        words = line.split()
        if len(words) != 2 or not all(word.isdigit() for word in words):
            return None
        rows.append((int(words[0]), int(words[1])))
    if len(rows) != 2:
        return None
    (job_device, job), (docker_device, docker) = rows
    return Disks(job, docker, job_device == docker_device)


def largest_archive(disks, on_job_disk=1):
    """Return the largest archive a restore may take with *disks* free.

    :param Disks disks: what the host has free
    :param int on_job_disk: copies of the archive that land on the job's
        disk: 1 for a download, 2 for an upload through a door, which the
        restore copies out of it
    :return: bytes, ``0`` when there is no room
    :rtype: int
    """
    if disks.shared:
        return max(
            0, (disks.free() - RESERVE_BYTES) // (on_job_disk + UNPACK_FACTOR),
        )
    return max(0, min(
        disks.job // on_job_disk,
        (disks.docker - RESERVE_BYTES) // UNPACK_FACTOR,
    ))


def room_refusal(disks, archive_bytes, copies, what):
    """Return why an archive on the host cannot be restored, or ``''``.

    :param Disks disks: what the host has free
    :param int archive_bytes: the archive's size
    :param int copies: further copies of it the restore makes on the
        job's disk first (``room_needed``)
    :param str what: the archive, as the message names it
    :rtype: str
    """
    archive_bytes = int(archive_bytes)
    if disks.shared:
        needed = room_needed(archive_bytes, copies)
        if disks.free() < needed:
            return no_room_message(disks.free(), needed, what)
        return ""
    needed = archive_bytes * copies
    if disks.job < needed:
        return (
            f"Not enough free disk on the server for {what} in /tmp: "
            f"{in_gib(disks.job)} free, {in_gib(needed)} needed for its "
            "copy. Nothing was restored: free space in /tmp on the server, "
            "or restore a smaller archive."
        )
    needed = RESERVE_BYTES + archive_bytes * UNPACK_FACTOR
    if disks.docker < needed:
        return (
            f"Not enough free disk on the server for {what} on the disk "
            f"its instances are kept on: {in_gib(disks.docker)} free, "
            f"{in_gib(needed)} needed ({UNPACK_FACTOR} times its size to "
            f"unpack it, and {in_gib(RESERVE_BYTES)} the server's other "
            "instances keep). Nothing was restored: free space on the "
            "server, or restore a smaller archive."
        )
    return ""


def described(disks):
    """Return what *disks* have free, as a message says it.

    :param Disks disks: what the host has free
    :rtype: str
    """
    if disks.shared:
        return f"{in_gib(disks.free())} free"
    return (
        f"{in_gib(disks.job)} free in /tmp, {in_gib(disks.docker)} on the "
        "disk of the instances"
    )
