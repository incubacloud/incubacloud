"""Post-migrate for 1.0.19 — backfill backup ``kind`` + new cron to bot.

**Backup kind.** ``cloud.instance.backup`` historically mixed two
semantics by convention: duplicity chain sets (production, synced by
``backup_list``) and one-shot non-production ZIP archives. The new
``kind`` field makes the split explicit; the ORM has just added the
column with its default (``chain``) on every existing row, so re-tag as
``archive`` the rows that are demonstrably archives: any row with a
download attachment (one-shot dumps, including production pre-restore
safety dumps), and every row of a non-production instance (duplicity
chains only ever exist on production). Rows whose archive attachment
already expired on a production instance stay ``chain`` — their
artifact is gone either way, and the next ``backup_list`` sync prunes
them like any stale set.

**Cron.** 1.0.19 also ships a new cron (job duration watch); the
post-init hook that points module crons at the bot only fires on
install, so re-run the idempotent helper (pattern of 1.0.15/1.0.16).
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    cr.execute(
        """
        UPDATE cloud_instance_backup b
        SET kind = 'archive'
        FROM cloud_instance i
        WHERE b.instance_id = i.id
          AND b.kind = 'chain'
          AND (b.attachment_id IS NOT NULL OR i.environment != 'production')
        """
    )
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
