"""Pre-migrate for 1.0.17 — take over the ``host_hardening`` job type.

The in-host hardening recipe was repatriated to core, which now ships its
own ``cloud.job.type`` with ``code='host_hardening'``. On databases where
another module owned that record before (``incubacloud_tenant`` shipped
it; a SaaS deployment may differ), the row is still there when core loads
its data — and ``cloud.job.type`` has a ``UNIQUE (code)`` constraint.

The result is not a warning: the insert fails, the registry fails to
load, and the database will not start. It has to be fixed **here**,
in core's own pre-migrate, because core is a dependency and loads before
any module that might have owned the record — a migration in that other
module would run far too late.

Verified by rehearsing the upgrade on a tenant database seeded with the
old ownership: without this it dies with
``duplicate key value violates unique constraint
"cloud_job_type_code_uniq"``. A fresh install never reproduces it, which
is why an install-only check missed it.

Re-pointing ``ir.model.data`` keeps the row's id, so every ``cloud.job``
already referencing that job type stays valid, and core's data load then
updates the same row instead of inserting a rival. Idempotent: the UPDATE
matches nothing on a second run, and it is skipped entirely if core
already owns a record.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Re-point a foreign-owned host_hardening job type at core."""
    if not version:
        return
    cr.execute("""
        UPDATE ir_model_data
           SET module = 'incubacloud'
         WHERE model = 'cloud.job.type'
           AND name = 'host_hardening'
           AND module != 'incubacloud'
           AND NOT EXISTS (
                 SELECT 1 FROM ir_model_data core
                  WHERE core.model = 'cloud.job.type'
                    AND core.name = 'host_hardening'
                    AND core.module = 'incubacloud'
               )
    """)
    if cr.rowcount:
        _logger.info(
            "host_hardening job type taken over by core (%s row)",
            cr.rowcount,
        )
