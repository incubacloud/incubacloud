"""Post-migrate for 1.0.229 — secrets of notification channels that are off go.

From this version a user's Telegram channel is on while a chat ID is
saved and the webhook while a URL is, and a save that leaves the address
empty deletes the bot token or the signing secret. A token saved without
a chat ID (the panel's Detect stored it before asking Telegram) or a
secret left behind its URL serves no notification; they are deleted as
that save now deletes them.

Idempotent: a second run finds none.

The backup paths saved outside the grammar the panel's destinations are
written in from this version (``_backup_dst.dst_is_valid``), an
instance's ``custom_backup_dst`` or the path a destination's bucket and
path compute, are named in the log, each with what to do, and kept: the
check holds for what is written from now on, and changing a path is
changing where a copy is looked for.
"""
import logging

from odoo.addons.incubacloud.models._backup_dst import dst_is_valid

_logger = logging.getLogger(__name__)


def _log_backup_dsts_outside_the_grammar(cr):
    """Name the backup paths the field's grammar would refuse; keep them.

    :param cr: database cursor
    :return: how many instances and destinations were named
    :rtype: tuple
    """
    cr.execute(
        """
        SELECT id, COALESCE(name->>'en_US', name::text), custom_backup_dst
          FROM cloud_instance
         WHERE NULLIF(custom_backup_dst, '') IS NOT NULL
        """
    )
    instances = [row for row in cr.fetchall() if not dst_is_valid(row[2])]
    for inst_id, name, dst in instances:
        _logger.warning(
            "1.0.229: instance %s (id %s) keeps a backup path outside the "
            "grammar the field now holds: %r. An administrator corrects it "
            "to where its copy is (boto3+s3://bucket/path) or empties it; "
            "until then a command quotes it and an environment or compose "
            "file refuses it if it breaks a line.",
            name, inst_id, dst,
        )
    cr.execute(
        """
        SELECT id, name, backup_dst
          FROM cloud_backup_backend
         WHERE NULLIF(backup_dst, '') IS NOT NULL
        """
    )
    backends = [row for row in cr.fetchall() if not dst_is_valid(row[2])]
    for backend_id, name, dst in backends:
        _logger.warning(
            "1.0.229: backup destination %s (id %s) computes a backup path "
            "outside the grammar its bucket and path now hold: %r. An "
            "administrator corrects the bucket and the path; until then "
            "its instances' paths are quoted in a command and refused by an "
            "environment or compose file if they break a line.",
            name, backend_id, dst,
        )
    return len(instances), len(backends)


def migrate(cr, version):
    """Delete the bot tokens and signing secrets of channels that are off.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    cr.execute(
        """
        UPDATE res_users
           SET cloud_telegram_bot_token = NULL
         WHERE NULLIF(cloud_telegram_bot_token, '') IS NOT NULL
           AND NULLIF(btrim(cloud_telegram_chat_id), '') IS NULL
        """
    )
    tokens = cr.rowcount
    cr.execute(
        """
        UPDATE res_users
           SET cloud_webhook_secret = NULL
         WHERE NULLIF(cloud_webhook_secret, '') IS NOT NULL
           AND NULLIF(btrim(cloud_webhook_url), '') IS NULL
        """
    )
    secrets = cr.rowcount
    if tokens or secrets:
        _logger.info(
            "1.0.229: %d Telegram bot token(s) without a chat ID and %d "
            "webhook secret(s) without a URL deleted", tokens, secrets,
        )
    _log_backup_dsts_outside_the_grammar(cr)
