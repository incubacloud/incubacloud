"""Post-migrate for 1.0.200 -- the direct path, without re-applying anyone.

Every host whose agents are installed gets ``metrics_agents_write_url`` =
the remote-write URL in force, which is what they were installed with:
the reconciliation compares that field with the URL each host should
have, and an empty one would re-apply the whole fleet on the first tick.
Archived hosts too: they come back as they were.

Also points the new cron (``cron_sync_direct_path``) at the cron bot, as
every update that adds a cron does.

A file of its own, beside ``post-migrate.py``: Odoo runs every
``post-*.py`` of the version's folder.

Idempotent: only a host with no URL recorded is touched.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Record the URL the installed agents use; reassign the new cron.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_assign_cron_user_id(module_name="incubacloud")
    cr.execute("""
        UPDATE cloud_host
           SET metrics_agents_write_url = (
                   SELECT metrics_remote_write_url FROM cloud_settings
                    ORDER BY id LIMIT 1)
         WHERE metrics_agents_state = 'installed'
           AND metrics_agents_write_url IS NULL
    """)
