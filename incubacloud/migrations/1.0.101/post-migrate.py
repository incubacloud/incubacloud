"""Post-migrate for 1.0.101 — own the new webhook-silence cron.

``_post_init_hook`` assigns this module's crons to the cron bot, but it
only runs on install. A cron added by an update therefore stays on the
implicit OdooBot owner, which in Odoo 19 also means its server action
carries none of the ``group_ids`` the execution pre-check needs — so it
would fail on every tick instead of watching for webhook silence.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Re-run the cron-bot assignment so the new cron is owned and scoped."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
