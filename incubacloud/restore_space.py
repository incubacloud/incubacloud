"""How much of a host's disk a restore may take, in one place.

A restore lands its archive on the host, in :data:`STAGING_ROOT`, and unpacks it into
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

The archive lands in :data:`STAGING_ROOT`; it is unpacked and loaded
inside the instance's containers, on the disk Docker keeps them on
(``DockerRootDir``). The two are one disk on most servers, but may be
two: so a restore measures both (:func:`disks_command`, :class:`Disks`):
on one disk it needs all of the above there; on two, room for the
archive and its copies where it lands, and the unpacking on Docker's,
each disk keeping the reserve (:func:`largest_archive`,
:func:`room_refusal`): where the archive lands is the server's root disk
then, which its system and its logs write to.
"""
import shlex
from collections import namedtuple

#: Where a restore lands its archive on the host: the upload doors'
#: directories, the path a file is copied to, and the restore's own
#: working directory. Not ``/tmp``: Debian 13 and Ubuntu since 24.10 mount
#: it as a tmpfs of half the memory by default (systemd's ``tmp.mount``),
#: 1.9 GiB on a server of 4 GB, and an archive larger than that could not
#: be restored by any path however free the instances' disk was
#: (18-nov-2026). ``/var/tmp`` is on disk wherever ``/tmp`` is a tmpfs,
#: and on most servers the disk Docker keeps the instances on; like
#: ``/tmp``, it is open to every account, sticky, so every check of who
#: owns what in it holds as it did there. Where it is another disk, the
#: restore measures it as such (:class:`Disks`).
STAGING_ROOT = "/var/tmp"

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

    :ivar int job: bytes free on the disk the archive lands on
        (:data:`STAGING_ROOT`)
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

    :param str path: a directory on the host (the job's, or
        :data:`STAGING_ROOT`)
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
        (disks.job - RESERVE_BYTES) // on_job_disk,
        (disks.docker - RESERVE_BYTES) // UNPACK_FACTOR,
    ))


def room_shortfall(disks, archive_bytes, copies):
    """Return where an archive on the host lacks room, or None.

    What :func:`room_refusal` says, as values, for a message in another
    language: ``(where, free, needed)``, *where* ``"disk"`` for the one
    disk both share, ``"staging"`` for the disk the archive lands on
    (:data:`STAGING_ROOT`), ``"docker"`` for the instances'.

    :param Disks disks: what the host has free
    :param int archive_bytes: the archive's size
    :param int copies: further copies of it the restore makes on the
        job's disk first (``room_needed``)
    :rtype: tuple | None
    """
    archive_bytes = int(archive_bytes)
    if disks.shared:
        needed = room_needed(archive_bytes, copies)
        return ("disk", disks.free(), needed) if disks.free() < needed else None
    needed = RESERVE_BYTES + archive_bytes * copies
    if disks.job < needed:
        return ("staging", disks.job, needed)
    needed = RESERVE_BYTES + archive_bytes * UNPACK_FACTOR
    if disks.docker < needed:
        return ("docker", disks.docker, needed)
    return None


def room_refusal(disks, archive_bytes, copies, what):
    """Return why an archive on the host cannot be restored, or ``''``.

    :param Disks disks: what the host has free
    :param int archive_bytes: the archive's size
    :param int copies: further copies of it the restore makes on the
        job's disk first (``room_needed``)
    :param str what: the archive, as the message names it
    :rtype: str
    """
    short = room_shortfall(disks, archive_bytes, copies)
    if short is None:
        return ""
    where, free, needed = short
    if where == "disk":
        return no_room_message(free, needed, what)
    if where == "staging":
        return (
            f"Not enough free disk on the server for {what} in "
            f"{STAGING_ROOT}: {in_gib(free)} free, {in_gib(needed)} "
            f"needed (its copy, and {in_gib(RESERVE_BYTES)} kept free "
            f"there). Nothing was restored: free space in {STAGING_ROOT} "
            "on the server, or restore a smaller archive."
        )
    return (
        f"Not enough free disk on the server for {what} on the disk "
        f"its instances are kept on: {in_gib(free)} free, "
        f"{in_gib(needed)} needed ({UNPACK_FACTOR} times its size to "
        f"unpack it, and {in_gib(RESERVE_BYTES)} the server's other "
        "instances keep). Nothing was restored: free space on the "
        "server, or restore a smaller archive."
    )


def described(disks):
    """Return what *disks* have free, as a message says it.

    :param Disks disks: what the host has free
    :rtype: str
    """
    if disks.shared:
        return f"{in_gib(disks.free())} free"
    return (
        f"{in_gib(disks.job)} free in {STAGING_ROOT}, "
        f"{in_gib(disks.docker)} on the disk of the instances"
    )
