"""Post-migrate for 1.0.9 — reassign crons to the cron bot.

New crons added in this version (e.g. ``cron_recover_stuck_moves``)
are created by the XML loader with ``user_id`` defaulting to OdooBot
(uid 1). The post-init hook only runs on install, not update, so
re-running ``_incubacloud_assign_cron_user_id`` here ensures the new
cron runs as the bot and the regression test passes.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
