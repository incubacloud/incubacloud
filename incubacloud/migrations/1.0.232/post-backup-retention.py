"""Post-migrate for 1.0.232: backup destinations' retentions in the form they must have.

From this version a destination's retention reaches the backup container
(the compose override) and only ``<count><unit>`` with D, W, M or Y in
capitals is accepted (``_backup_retention.valid_backup_retention``). What
is saved is put in that form where it means the same to the panel:
spaces around it go, the unit goes to capitals (the panel's day count
already read ``1m`` as a month) and an empty one becomes ``3M``, which
every reader took it for. Anything else is left as it is and named in the
log: the update goes on, the deploy writes no cleanup for it and the
template's ``3M`` stays until someone fixes it.

Idempotent: a second run changes nothing.
"""
import logging

from odoo.addons.incubacloud.models._backup_retention import (
    TEMPLATE_BACKUP_RETENTION,
    valid_backup_retention,
)

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Normalise the stored retentions and log the ones left invalid.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    cr.execute("SELECT id, name, backup_retention FROM cloud_backup_backend")
    for backend_id, name, stored in cr.fetchall():
        value = (stored or "").strip().upper() or TEMPLATE_BACKUP_RETENTION
        if not valid_backup_retention(value):
            _logger.warning(
                "1.0.232: backup destination %s (%r) keeps the retention %r,"
                " which is not a count followed by D, W, M or Y: the backup"
                " cleanup ignores it and keeps three months until it is"
                " fixed", backend_id, name, stored,
            )
            continue
        if value != stored:
            cr.execute(
                "UPDATE cloud_backup_backend SET backup_retention = %s"
                " WHERE id = %s",
                (value, backend_id),
            )
            _logger.info(
                "1.0.232: backup destination %s (%r) retention %r written as %r",
                backend_id, name, stored, value,
            )
