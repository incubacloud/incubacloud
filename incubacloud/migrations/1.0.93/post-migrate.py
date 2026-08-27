"""Post-migrate for 1.0.93 — assign the archive-check cron to the bot.

Same reason as 1.0.15, 1.0.17, 1.0.40 and 1.0.90:
``_incubacloud_assign_cron_user_id`` runs from the post-init hook, which
fires on **install** only, so every version that ships a new cron needs
one of these. On an upgraded database the new cron would otherwise keep
the implicit OdooBot owner. ``TestCronBotUser.test_all_module_crons_run_as_bot``
is the guard that catches a forgotten one.

The new cron is "Verify archived instance copies": it lists each
archived instance's frozen prefix and stamps what it found, so a copy
that a provider lifecycle or a manual delete removed is discovered
before someone presses "revive" and finds nothing there.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Point module crons added since the last install at the cron bot."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
