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

So are the values the destinations' and the instances' new checks would
refuse, kept as they are: a destination's endpoint, keys, passphrase,
image tag, time zone and addresses
(``cloud.backup.backend._check_s3_access_shape`` and
``_check_backup_answers_shape``), a secret named without its value; and
an instance's backup path that overlaps a destination's or another
instance's (``cloud.instance._check_custom_backup_dst_shape``). Until
they are corrected, a file they would break is refused before the host
is reached, and a purge that would reach another's copies is refused.
A passphrase or a key that compose reads as more than its value in
``backup.env`` or ``pitr.env`` (a quote, ``$``, ``#``…) is named too:
until it is corrected the rebuild refuses the file.

The overlaps are read before ``incubacloud_pitr`` is loaded in this
update, so they are core's backup paths alone: one with a continuous
recovery prefix is not named here, and the purge's own check refuses it
all the same. Only the log misses it.
"""
import logging

import pytz

from odoo import SUPERUSER_ID, api
from odoo.tools import email_normalize

from odoo.addons.incubacloud.models._backup_dst import (
    access_key_is_valid,
    breaks_line,
    compose_env_breaks,
    dst_is_valid,
    endpoint_url_is_valid,
    image_tag_is_valid,
)

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


def _backend_values_outside_their_checks(backend):
    """Name what a destination holds that its checks now refuse.

    :param backend: a ``cloud.backup.backend``, as the superuser
    :return: one description per value; a secret's without its value
    :rtype: list
    """
    found = []
    url = backend.s3_endpoint_url
    if url and not endpoint_url_is_valid(url):
        found.append(f"endpoint URL {url!r}")
    for label, fname, refused, what in (
        ("access key ID", "s3_access_key_id",
         lambda v: not access_key_is_valid(v),
         "a character other than letters, digits and / + = . _ -"),
        ("secret access key", "s3_secret_access_key",
         lambda v: not access_key_is_valid(v),
         "a character other than letters, digits and / + = . _ -"),
        ("passphrase", "passphrase", compose_env_breaks,
         "a quote, a $, a backslash, a #, a space at either end, a line "
         "break or a control character, which compose does not read as "
         "written"),
    ):
        try:
            value = backend[fname]
        except Exception:  # noqa: BLE001 - unreadable: its own alert says so
            continue
        if value and refused(value):
            found.append(f"{label} (with {what})")
    tag = backend.backup_image_version
    if tag and not image_tag_is_valid(tag):
        found.append(f"image version {tag!r}")
    if backend.backup_tz and backend.backup_tz not in pytz.all_timezones_set:
        found.append(f"timezone {backend.backup_tz!r}")
    for label, address in (
        ("notification from", backend.email_from),
        ("notification to", backend.email_to),
    ):
        if address and (breaks_line(address) or not email_normalize(address)):
            found.append(f"{label} {address!r}")
    return found


def _log_backup_values_outside_their_checks(cr):
    """Name the destinations' and instances' values the new checks refuse; keep them.

    :param cr: database cursor
    :return: how many destinations and instances were named
    :rtype: tuple
    """
    env = api.Environment(cr, SUPERUSER_ID, {"active_test": False})
    backends = 0
    for backend in env["cloud.backup.backend"].search([]):
        found = _backend_values_outside_their_checks(backend)
        if not found:
            continue
        backends += 1
        _logger.warning(
            "1.0.229: backup destination %s (id %s) keeps values its checks "
            "now refuse: %s. An administrator corrects them; until then a "
            "rebuild or a purge that would write one into a file refuses it "
            "before the host is reached.",
            backend.name, backend.id, "; ".join(found),
        )
    instances = 0
    for inst in env["cloud.instance"].search([("custom_backup_dst", "!=", False)]):
        dst = inst.custom_backup_dst
        if not dst_is_valid(dst) or dst == inst._computed_backup_dst():
            continue
        conflict = inst._backup_prefix_conflict(dst, held=True)
        if conflict is None:
            continue
        instances += 1
        dest, other = conflict
        reached = (
            f"the backup path of instance {other.name} (id {other.id})"
            if other else f"the path of backup destination {dest.name or '?'}"
        )
        _logger.warning(
            "1.0.229: instance %s (id %s) keeps the backup path %r, which "
            "overlaps %s. An administrator gives it a path of its own; until "
            "then a purge that would reach the other's copies is refused.",
            inst.name, inst.id, dst, reached,
        )
    return backends, instances


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
    _log_backup_values_outside_their_checks(cr)
