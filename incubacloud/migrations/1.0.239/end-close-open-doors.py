"""End-migrate for 1.0.239 — the upload doors open while the panel updates.

A door opened before 1.0.239 writes to ``/tmp``, a tmpfs of half the
memory on Debian 13 and Ubuntu since 24.10, under a key whose command
takes any number of files, each up to the door's limit. From 1.0.239 a
door writes to ``/var/tmp`` and takes one file, and the restore reads only
there: what an older door received could not be restored, and the key
would go on taking files past what the server's disk can hold.

So every door open is closed, as its revocation does, and the Import
dialog says why (``cloud.restore.upload.grant._close_for_update``). Simpler
than telling each door's age apart where it is shown, and nothing open
keeps the old command. ``end``: the revocation queues a job, with every
module loaded. One savepoint: a failure is logged and the update goes on;
the doors then close at their deadline, as before.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Close the upload doors opened before this release.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        with cr.savepoint():
            closed = env["cloud.restore.upload.grant"]._close_for_update()
    except Exception:  # the doors close at their deadline instead
        _logger.exception("Upload doors: closing those open failed")
        return
    if closed:
        _logger.info("Upload doors: closed %d opened before 1.0.239", len(closed))
