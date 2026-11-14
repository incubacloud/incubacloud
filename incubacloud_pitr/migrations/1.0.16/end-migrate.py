"""End-migrate for incubacloud_pitr 1.0.16 — archives switched off get a purge date.

From this version the archive of an instance switched off is emptied once
the backup window it had passed (``pitr_purge``), and every rebuild keeps
the prefix and destination it rendered (``pitr_rendered_prefix``,
``pitr_rendered_backend_id``). Before, an archive switched off stayed in
the bucket for as long as the instance lived.

* Every active instance whose stack writes ``pitr.env`` (archiving, or switched
  off with a cluster) gets the prefix and destination computed now: what
  its last rebuild rendered, unless its destination changed since, which
  nothing recorded.
* Every instance switched off with a cluster gets its purge date
  (``_pitr_schedule_leftover_purges``), from now: whoever switched it off
  did so when the archive stayed, and gets the whole window from the
  update to switch it on again and keep it. Dated from the switch off, an
  archive switched off longer than the window ago would go minutes after
  the update.

``end`` and not ``post``: a layer's override of the window
(``_pitr_window_days``, a plan's retention) has to be loaded
(``odoo/modules/loading.py`` runs the ``end-*`` scripts with every module
loaded). Idempotent: a second run finds them done.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Record the rendered prefixes, then date the archives switched off.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Instance = env["cloud.instance"]
    rendered = 0
    for inst in Instance.search([
        ("pitr_rendered_prefix", "=", False),
        "|", ("pitr_enabled", "=", True), ("pitr_cluster_id", "!=", False),
    ]):
        if not inst._pitr_writes_env():
            continue
        backend, prefix = inst._pitr_purge_target()
        if prefix:
            inst.write({
                "pitr_rendered_prefix": prefix,
                "pitr_rendered_backend_id": backend.id,
            })
            rendered += 1
    scheduled = Instance._pitr_schedule_leftover_purges()
    _logger.info(
        "incubacloud_pitr 1.0.16: %d rendered recovery prefix(es) recorded, "
        "%d archive(s) switched off given a purge date", rendered, scheduled,
    )
