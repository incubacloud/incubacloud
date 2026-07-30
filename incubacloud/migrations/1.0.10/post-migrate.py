"""Post-migrate for 1.0.10.

1. Backfill ``cloud.backup.backend.retention_owner`` from the old
   ``deletion_via_cron`` Boolean (P15 #9a). Odoo does not drop columns
   for removed fields, so the old column is still present at this point
   (``_auto_init`` has already added the two new columns with their
   model defaults). Backends that had the flag on are unambiguous
   (cron); backends with the flag off are ambiguous — we don't know
   whether a bucket lifecycle policy exists — so they're marked
   undeclared and the next run of ``_cron_check_retention_declared``
   raises a warning alert.
2. Three new crons were added in this version (retention_declared,
   managed_backend_unused, production_no_backup). Same as 1.0.9: the
   XML loader creates them with ``user_id`` defaulting to OdooBot, so
   re-run ``_incubacloud_assign_cron_user_id`` to put them on the bot.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'cloud_backup_backend'
          AND column_name = 'deletion_via_cron'
    """)
    if cr.fetchone():
        cr.execute("""
            UPDATE cloud_backup_backend
               SET retention_owner = CASE
                       WHEN deletion_via_cron THEN 'cron' ELSE 'lifecycle'
                   END,
                   retention_owner_declared = deletion_via_cron
        """)
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["cloud.backup.backend"].invalidate_model(
        ["retention_owner", "retention_owner_declared"],
    )
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
