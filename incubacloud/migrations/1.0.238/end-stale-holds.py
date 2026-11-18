"""End-migrate for 1.0.238 — the queues a conflict closed before 1.0.220 holds.

Until 1.0.220 closing a conflict alert (``pip_conflict``,
``addon_conflict``) left open the ``job_failed`` of the job that failed on
it, and that alert held the instance's queue: what was asked for behind it
never started (an Odoo 18 instance on a panel, a rebuild and a
listing of backups waiting a week, 9-nov-2026). 1.0.220 closes both from
then on (``cloud.alert._close_conflict_failures``) and left the pairs that
were already there.

Each such failure is closed now, ``auto``, and what waited behind it is
cancelled with a note that says to ask for it again
(``cloud.job._close_stale_conflict_holds_on``): it was asked for days
before, and an update must not start it unannounced. What passes the hold
anyway, a queue still held by another failure and the heads of the queue
are left alone.

``end`` and not ``post``: the cancel runs each executor's hook and the
close the layers' own, with every module loaded. One savepoint for the
read and one per queue: a queue that fails is logged with the alerts to
dismiss by hand, and the update goes on. The note is written in English:
the update runs in no one's language, as the dispatcher does.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Close the failures left holding a queue after their conflict was closed.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Job = env["cloud.job"]
    try:
        with cr.savepoint():
            scopes = Job._stale_hold_scopes()
    except Exception:  # the update goes on with the queues as they are
        _logger.exception("Stale conflict holds: reading them failed")
        return
    for scope, alert_ids in scopes:
        try:
            with cr.savepoint():
                Job._close_stale_conflict_holds_on(scope)
        except Exception:  # this queue stays as it was; the rest go on
            _logger.exception(
                "Stale conflict holds: instance %s could not be released;"
                " dismiss alert(s) %s by hand",
                scope[0], ", ".join(map(str, alert_ids)),
            )
