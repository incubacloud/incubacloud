"""Post-migrate for 1.0.231 — the GeoIP refresh and the reading of the
servers' security events run as the cron bot.

``cron_geoip_refresh`` and ``cron_security_events`` arrive with this
version and, like every cron of this module on an existing database, land
owned by uid 1: they are put on the platform bot, as in 1.0.221.

Idempotent: the assignment skips crons that already point at the bot.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Give the new GeoIP and security events crons to the cron bot.

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
