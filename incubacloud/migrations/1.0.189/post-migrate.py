"""Post-migrate for 1.0.189 — put the new crons on the platform bot.

Two crons arrive with this version: the daily capture of instance
encryption keys (``cron_capture_secret_keys``) and the hourly pass that
closes alerts on secrets that read again
(``cron_resolve_readable_secrets``). On an existing database they land
owned by uid 1: ``_incubacloud_assign_cron_user_id`` only runs from the
post-init hook on install.

The capture's first run, right after the upgrade, queues one reading per
instance deployed before the panel kept their keys; nothing else here
touches data.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Enrol this module's crons still on uid 1 with the platform bot."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
