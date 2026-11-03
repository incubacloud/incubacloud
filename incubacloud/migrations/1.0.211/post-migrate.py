"""Post-migrate for 1.0.211 — the move between hosts freezes its old copy,
and an instance has a scheduling priority.

``scheduling_priority`` arrives with this version and is required: every
instance that has none takes ``normal``, the weight Docker gives a
container by default, so nothing changes for it until a layer above sets
another.

``cron_clean_frozen_move_sources`` arrives too and, like every cron of
this module on an existing database, lands owned by uid 1: it is put on
the platform bot, as in 1.0.111, 1.0.198 and 1.0.200.

Idempotent: only empty priorities are written, and the assignment skips
crons that already point at the bot.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Give every instance a priority, and the new cron to the cron bot.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    cr.execute(
        "UPDATE cloud_instance SET scheduling_priority = 'normal' "
        "WHERE scheduling_priority IS NULL"
    )
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_ensure_cron_bot()
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
