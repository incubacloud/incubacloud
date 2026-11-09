"""Post-migrate for 1.0.221 — the daily check of Git provider tokens and
the daily reprocessing and purge of GitLab webhook events run as the cron
bot.

``cron_check_git_providers`` and ``cron_purge_gitlab_events`` arrive with
this version and, like every cron of this module on an existing database,
land owned by uid 1: they are put on the platform bot, as in 1.0.213.

Idempotent: the assignment skips crons that already point at the bot.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Give the new Git provider and GitLab event crons to the cron bot.

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
