"""End-migrate for 1.0.234: check every running instance's admin password.

From this version every flow that puts a database under an instance's
Odoo replaces Odoo's default admin password, if the database still has it,
with a random one that is never kept (``models/_admin_password.py``). The
instances deployed before have never been checked: this queues one
``check_admin_password`` job for each that is deployed and running, as the
platform's and visible like any other job, each in a savepoint of its
own: one that cannot be queued leaves the update and the others as they
were. Nothing is changed here; each job changes the password only if it
still verifies ``admin``.

Stopped instances are left alone: they are checked when they start
(``start_instance``). The hourly sweep (``cron_check_admin_passwords``)
queues again the ones whose check could not run or failed.

``end`` and not ``post``: a layer that excludes its own instances
(``_admin_password_check_domain``) is loaded by then.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Queue the check of every running instance.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    queued = env["cloud.instance"]._queue_admin_password_checks(limit=None)
    _logger.info("1.0.234: queued the admin password check of %d instances", queued)
