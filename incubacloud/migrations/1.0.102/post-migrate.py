"""Post-migrate for 1.0.102 — own the adopted and new crons.

``_post_init_hook`` assigns this module's crons to the cron bot, but it
only runs on install. A cron added — or, here, adopted — by an update
therefore stays on the implicit OdooBot owner, which in Odoo 19 also
means its server action carries none of the ``group_ids`` the execution
pre-check needs, and it fails on every tick.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Re-run the cron-bot assignment so every core cron is owned and scoped."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
