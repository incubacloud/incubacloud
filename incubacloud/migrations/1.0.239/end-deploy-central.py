"""End-migrate for 1.0.239 — the central deployed again for vmauth's mount.

From 1.0.239 vmauth loads a new account list in place, which needs its
configuration's directory mounted rather than the file
(``CENTRAL_REVISION`` 5). Until the central is deployed again, an account
sync refuses to reload a vmauth that mounts the file alone and asks for
that deployment. The reconciliation cron queues it within its interval;
queued here as the update ends, the wait is the queue's alone. The
deployment recreates vmauth once.

``end``: the queue needs every module loaded. One savepoint: a failure is
logged and the update goes on; the cron queues it then.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Queue the central's deployment when it is behind this code.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        with env.cr.savepoint():
            env["cloud.host"]._reconcile_central_revision()
    except Exception as exc:  # noqa: BLE001 — the cron tries again
        _logger.warning(
            "[metrics] the central's deployment is left to the cron: %s", exc,
        )
