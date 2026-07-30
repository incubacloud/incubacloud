"""Post-migrate for 1.0.11.

Drop the tables of the two models removed in the dead-code cleanup
(``cloud.job.log.message`` and ``cloud.connect.token``). Odoo's
``_process_end`` removes the ``ir.model`` rows, the ACLs and the record
rules when the module is updated, but it leaves the tables behind, so
they linger as orphans forever.

Both were verified empty before removal: the log-message model never had
a single write (all job output goes through ``cloud.job.log.chunk``) and
the connect token was a TransientModel vacuumed after ~1 minute.
"""

import logging

_logger = logging.getLogger(__name__)

_DROPPED_TABLES = ("cloud_job_log_message", "cloud_connect_token")


def migrate(cr, version):
    """Drop the orphan tables of the two removed models."""
    for table in _DROPPED_TABLES:
        cr.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table,),
        )
        if not cr.fetchone():
            continue
        cr.execute(f'SELECT COUNT(*) FROM "{table}"')
        rows = cr.fetchone()[0]
        if rows:
            # Never silently discard data: if a deployment somehow has
            # rows here, leave the table for an operator to inspect.
            _logger.warning(
                "incubacloud 1.0.11: %s still holds %s row(s) — table kept, "
                "drop it manually once reviewed",
                table, rows,
            )
            continue
        cr.execute(f'DROP TABLE "{table}"')
        _logger.info("incubacloud 1.0.11: dropped orphan table %s", table)
