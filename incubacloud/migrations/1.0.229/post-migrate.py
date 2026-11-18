"""Post-migrate for 1.0.229 — secrets of notification channels that are off go.

From this version a user's Telegram channel is on while a chat ID is
saved and the webhook while a URL is, and a save that leaves the address
empty deletes the bot token or the signing secret. A token saved without
a chat ID (the panel's Detect stored it before asking Telegram) or a
secret left behind its URL serves no notification; they are deleted as
that save now deletes them.

Idempotent: a second run finds none.
"""
import logging

_logger = logging.getLogger(__name__)


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
