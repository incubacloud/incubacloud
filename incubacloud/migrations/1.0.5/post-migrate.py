"""Post-migrate for 1.0.5 — authorize bot crons for Odoo 19's hardened
server-action access check.

Odoo 19's ``ir.actions.server._can_execute_action_on_records`` requires
*write* access to the action's model when the action carries no
``group_ids``. Several module crons run as the cron bot against
append-only / abstract models it cannot write (audit log, rate-limit
buckets, terminal routes), so they failed with ``AccessError`` on every
tick — before their internal ``sudo()`` ever ran. Re-running the
cron-assignment helper now also scopes each bot cron's server action to
``group_cloud_manager`` so the check passes via the group-membership
branch.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Re-apply the cron-bot assignment so existing installs get the
    server-action ``group_ids`` that authorise the Odoo 19 pre-check."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
