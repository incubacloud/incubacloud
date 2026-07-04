"""Post-migrate for 1.0.7 — assign the new notification crons to the bot.

The alert-GC and daily-digest crons ship in this version. ``ir.cron``
records load with ``user_id`` defaulting to uid 1 (OdooBot), but the
cron-assignment helper only runs from the install hook, not on update —
so on existing installs these two crons would keep running as uid 1
instead of the scoped cron bot. Re-run the helper to switch them (and
to scope their server actions to ``group_cloud_manager`` for Odoo 19's
hardened action pre-check, same as 1.0.5).
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Re-apply the cron-bot assignment so the new crons run as the bot."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
