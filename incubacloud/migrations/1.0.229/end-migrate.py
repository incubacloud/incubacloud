"""End-migrate for 1.0.229 — each server's own Send security logs.

The panel-wide *Send hosts' security logs* (``metrics_ship_security_logs``)
gives way to a switch on every server, ``cloud.host.security_logs_enabled``.
The column is created with its default for every host; this sets the value
the update decides, ``cloud.host._security_logs_after_upgrade``: on
everywhere, also where the panel-wide setting was off (the owner's
decision of 10-nov-2026, said in the changelog), unless a layer whose
panels belong to others keeps what they had. The old column stays in the
table, unread.

Written with SQL, not ``write``: the agents' revision (2) already has the
reconciliation cron re-apply every installed server, five per pass, and
a write would queue them all at once.

``end`` and not ``post``: a layer's override has to be loaded
(``odoo/modules/loading.py`` runs the ``end-*`` scripts with every module
loaded).
"""
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools.sql import column_exists

_logger = logging.getLogger(__name__)


def _shipped_before(cr):
    """Return the panel-wide setting as it was before the update.

    :param cr: database cursor
    :return: whether the panel sent its hosts' security logs.
    :rtype: bool
    """
    if not column_exists(cr, "cloud_settings", "metrics_ship_security_logs"):
        return False
    cr.execute("SELECT bool_or(metrics_ship_security_logs) FROM cloud_settings")
    row = cr.fetchone()
    return bool(row and row[0])


def migrate(cr, version):
    """Give every server the switch's value the update decides.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    shipped = _shipped_before(cr)
    value = bool(env["cloud.host"]._security_logs_after_upgrade(shipped))
    cr.execute(
        "UPDATE cloud_host SET security_logs_enabled = %s "
        "WHERE security_logs_enabled IS DISTINCT FROM %s",
        (value, value),
    )
    cr.execute("SELECT count(*) FROM cloud_host")
    total = cr.fetchone()[0]
    _logger.info(
        "[metrics] Send security logs is %s on the %s server(s) of this panel "
        "(the panel-wide setting was %s)",
        "on" if value else "off", total, "on" if shipped else "off",
    )
    env["cloud.host"].invalidate_model(["security_logs_enabled"])
