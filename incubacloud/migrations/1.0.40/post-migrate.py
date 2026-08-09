"""Post-migrate for 1.0.40 — assign the reconciliation cron to the bot.

Same reason as 1.0.15 and 1.0.17: ``_incubacloud_assign_cron_user_id``
runs from the post-init hook, which fires on **install** only, so every
version that ships a new cron needs one of these. The module's own guard
(``TestCronBotUser.test_all_module_crons_run_as_bot``) is what catches a
forgotten one — it caught this one too.

The new cron is "Reconcile observability enrolment", which is what makes
the agents converge onto every host instead of being installed once and
forgotten.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Point module crons added since the last install at the cron bot."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
