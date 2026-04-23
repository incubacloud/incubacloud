"""Post-migrate for 1.0.2 — cron bot user + ownership.

The ``post_init_hook`` runs only on fresh install. Existing
installs upgraded to 1.0.2 need the same work: create the cron
bot user (if it does not already exist) and reassign every
``ir.cron`` owned by this module from the implicit OdooBot
(uid=1) to the bot.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_ensure_cron_bot()
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
