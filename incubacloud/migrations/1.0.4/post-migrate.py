"""Post-migrate for 1.0.4 — reassign new crons to the cron bot user.

``cron_measure_backup_usage`` (and any cron introduced in 1.0.3 that
forgot ``user_id``) was created with the default ``uid=1`` because
the XML omits ``user_id``.  Re-run the bot-assignment helper so the
regression guard ``test_all_module_crons_run_as_bot`` stays green
for existing installs upgrading past 1.0.3.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
