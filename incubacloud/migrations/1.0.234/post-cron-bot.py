"""Post-migrate for 1.0.234: the pending-restart check runs as the cron bot.

``cron_host_reboot_check`` arrives with 1.0.232 and, like every cron
of this module on an existing database, lands owned by uid 1: it is put on
the platform bot, as in 1.0.221 and 1.0.231 (QA of candidate S, 17-nov-2026:
``test_all_module_crons_run_as_bot`` on the copy of production).

Idempotent: the assignment skips crons that already point at the bot.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Give the new pending-restart cron to the cron bot.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_ensure_cron_bot()
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
