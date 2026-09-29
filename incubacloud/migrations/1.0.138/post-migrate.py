"""Post-migrate for 1.0.138 — put the host shell's GC cron on the bot.

On a database that already had the host shell, the pre-migrate handed
this module the existing cron, already on the bot. On one that did not,
the cron is new and lands owned by uid 1:
``_incubacloud_assign_cron_user_id`` runs from the post-init hook, which
only fires on **install**.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Enrol this module's crons still on uid 1 with the platform bot."""
    # Idempotent: it only rewrites crons still pointing at uid 1, so one
    # an operator deliberately re-routed keeps their choice.
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
