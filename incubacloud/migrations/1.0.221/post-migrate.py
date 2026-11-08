"""Post-migrate for 1.0.221 — the daily reprocessing and purge of GitLab
webhook events runs as the cron bot.

``cron_purge_gitlab_events`` arrives with this version and, like every
cron of this module on an existing database, lands owned by uid 1: it is
put on the platform bot, as in 1.0.220.

Idempotent: the assignment skips crons that already point at the bot.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Give the new GitLab event cron to the cron bot.

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
