"""Where a browser-uploaded restore archive waits for the job that sends it.

The upload arrives on an HTTP worker; the job that consumes it runs
somewhere else. Since the job runner was split out, ``odoo`` serves
requests with ``--max-cron-threads=0`` and ``odoo_runner`` executes the
queue, and the two containers share exactly one mount — the data
directory. ``/tmp`` is private to each, so a ``/tmp`` path handed across
in a job payload names a file that does not exist on the other side: the
executor rejects it as missing and the upload is stranded, since the only
code that would have deleted it lives on the side that cannot see it.

Staging under the data directory is what makes the handoff work at all.
Both halves import from here, so the writer and the validator cannot
drift apart — a test pins that they agree.
"""
import logging
import secrets
import time
from pathlib import Path

from odoo.tools import config

_logger = logging.getLogger(__name__)

#: Subdirectory of the data dir. Deliberately *not* inside ``filestore``:
#: Odoo's filestore GC walks that tree, and these files have no
#: ``ir.attachment`` pointing at them.
_STAGING_DIRNAME = "incubacloud-restore"

#: Age past which a staged upload is considered abandoned. Generous on
#: purpose: production forces a full backup of the target before the
#: restore, so a legitimate archive can wait hours for its turn.
STALE_AFTER_HOURS = 24


def staging_dir():
    """Return the staging directory, creating it 0700 if needed.

    :return: ``Path`` to the directory both containers can reach.
    """
    path = Path(config["data_dir"]) / _STAGING_DIRNAME
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _name_prefix(instance_id):
    """Return the filename prefix that marks an upload for *instance_id*."""
    return f"cloud_restore_{int(instance_id)}_"


def new_upload_path(instance_id):
    """Return a fresh, unused path to stage an upload for *instance_id*.

    The random suffix keeps two concurrent uploads for the same instance
    from colliding; the instance id in the name is what
    :func:`is_staged_upload` later checks the job payload against.

    :param instance_id: id of the ``cloud.instance`` being restored.
    :return: ``Path`` that does not exist yet.
    """
    return staging_dir() / (
        f"{_name_prefix(instance_id)}{secrets.token_hex(8)}.zip"
    )


def is_staged_upload(path, instance_id):
    """Return True if *path* is an upload this staging area owns.

    Checked before the executor reads or deletes anything, because the
    path arrives inside a job payload and ``restore_db`` is reachable
    over JSON-RPC: without this, a caller could name any file on the
    server and have it uploaded to a host and then unlinked.

    The parent directory is compared exactly rather than by string
    prefix, so a sibling directory whose name merely starts the same way
    cannot pass, and ``resolve()`` collapses symlinks and ``..`` first.

    :param path: candidate path from the payload.
    :param instance_id: instance the job is restoring.
    :return: bool
    """
    if not path:
        return False
    try:
        resolved = Path(path).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return False
    return (
        resolved.parent == staging_dir().resolve()
        and resolved.name.startswith(_name_prefix(instance_id))
    )


def purge_stale(max_age_hours=STALE_AFTER_HOURS):
    """Delete staged uploads older than *max_age_hours*.

    The executor removes the archive once it has been sent, but nothing
    covers the upload whose job never runs — cancelled before it
    started, or enqueued against an instance that was removed. Those
    would otherwise sit at up to 2 GiB each until the container is
    recreated.

    :param max_age_hours: age past which a file is considered abandoned.
    :return: number of files removed.
    """
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for entry in staging_dir().iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except OSError:
            _logger.warning(
                "restore staging: could not remove %s", entry, exc_info=True,
            )
            continue
        removed += 1
    if removed:
        _logger.info("restore staging: removed %d abandoned upload(s)", removed)
    return removed
