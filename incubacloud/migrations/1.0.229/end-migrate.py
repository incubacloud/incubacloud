"""End-migrate for 1.0.229 — security logs per server, a central without Grafana.

**Send security logs.**
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

**Grafana.** Its admin password and base URL are emptied: the columns
stay in the table, and a secret nothing uses is not left there. The
central is deployed again, which removes Grafana: queued here when it can
be, and in any case by the reconciliation cron while the panel's
``metrics_central_revision`` is behind (``_reconcile_central_revision``),
so a deployment run by older code, or one that fails, is tried again.

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


def _forget_grafana(cr):
    """Empty Grafana's admin password and base URL where the columns remain.

    :param cr: database cursor
    """
    for column in ("grafana_admin_password", "grafana_base_url"):
        if column_exists(cr, "cloud_settings", column):
            cr.execute(
                f"UPDATE cloud_settings SET {column} = NULL "  # noqa: S608 — fixed names
                f"WHERE {column} IS NOT NULL"
            )


def _deploy_the_central(env):
    """Queue the central's deployment now, when it can be.

    Never fails the update: the reconciliation cron queues it otherwise.

    :param env: an environment with every module loaded
    """
    try:
        with env.cr.savepoint():
            env["cloud.host"]._reconcile_central_revision()
    except Exception as exc:  # noqa: BLE001 — the cron tries again
        _logger.warning(
            "[metrics] the central's deployment is left to the cron: %s", exc,
        )


def migrate(cr, version):
    """Give every server the switch's value, and retire Grafana.

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
    _forget_grafana(cr)
    _deploy_the_central(env)
