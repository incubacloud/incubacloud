"""Post-migrate for 1.0.90 — assign the restore-staging GC cron to the bot.

Same reason as 1.0.15, 1.0.17 and 1.0.40: ``_incubacloud_assign_cron_user_id``
runs from the post-init hook, which fires on **install** only, so every
version that ships a new cron needs one of these. On an upgraded database
the new cron would otherwise keep the implicit OdooBot owner. The module's
own guard (``TestCronBotUser.test_all_module_crons_run_as_bot``) is what
catches a forgotten one — it caught this one too.

The new cron is "Garbage-collect abandoned restore uploads": the sweep for
browser-uploaded archives whose restore job never ran, which no ``finally``
in the executor can cover.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Point module crons added since the last install at the cron bot."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
