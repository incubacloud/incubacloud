"""Post-migrate for 1.0.200 — hand the hourly posture check to the cron bot,
and close the upload grants an older release opened.

``post_init_hook`` promotes every ``ir.cron`` of this module from the
implicit OdooBot to the dedicated cron user, and it runs only on a fresh
install. ``cron_check_host_posture`` (``models/cloud_host_posture.py``)
arrives with this version; on an existing database it lands owned by
uid 1, so it is put on the platform bot like every other cron of the
module, as in 1.0.111 and 1.0.198.

An upload grant still open was installed by an older release, with a
key line this version no longer writes (``-wo`` alone, see
``cloud.restore.upload.grant._authorized_keys_line``). Its deadline is
moved into the past and the daily sweep is asked to run as soon as the
upgrade is over: it revokes them the usual way, one queued job per
grant. Nothing is queued and no host is reached during the upgrade
itself, so a host that is down cannot fail it; its revocation fails
like any other, and the key stops being accepted at its own expiry.

Idempotent: the assignment skips crons that already point at the bot,
and a grant already closed is left as it is.
"""
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    """Reassign this module's crons to the cron bot user, and close the
    upload grants an older release opened.

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
    _close_open_upload_grants(env)


def _close_open_upload_grants(env):
    """Hand every open upload grant to the sweep, and have it run soon.

    :param env: superuser environment
    """
    grants = env["cloud.restore.upload.grant"].search([
        ("state", "=", "granted"),
    ])
    if not grants:
        return
    # A second back: the sweep revokes what expired strictly before now.
    grants.write({"expires_at": fields.Datetime.now() - timedelta(seconds=1)})
    env.ref("incubacloud.cron_cloud_restore_upload_grant_gc")._trigger()
