"""Post-migrate for 1.0.15 — assign the new observability cron to the bot.

``_incubacloud_assign_cron_user_id`` normally runs from the module's
post-init hook, which only fires on **install**. A cron shipped in a
later version therefore lands on an existing database still owned by
uid 1 (OdooBot) — the module's own regression guard
(``TestCronBotUser.test_all_module_crons_run_as_bot``) catches exactly
that, and caught this one.

Re-running the helper is idempotent: it only rewrites crons still
pointing at uid 1, so a cron an operator deliberately re-routed keeps
their choice.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Point module crons added since the last install at the cron bot."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
