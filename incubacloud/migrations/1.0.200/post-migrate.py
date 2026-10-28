"""Post-migrate for 1.0.200 — close the upload grants an older release opened.

An upload grant still open was installed by an older release, with a
key line this version no longer writes (``-wo`` alone, see
``cloud.restore.upload.grant._authorized_keys_line``). Its deadline is
moved into the past and the daily sweep is asked to run as soon as the
upgrade is over: it revokes them the usual way, one queued job per
grant. Nothing is queued and no host is reached during the upgrade
itself, so a host that is down cannot fail it; its revocation fails
like any other, and the key stops being accepted at its own expiry.

Idempotent: a grant already closed is left as it is.
"""
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    """Close the upload grants an older release opened.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
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
