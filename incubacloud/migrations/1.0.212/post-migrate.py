"""Post-migrate for 1.0.212 — a restore's link leaves its job's payload, and
every key that opened an upload door is remembered.

A restore from a link kept the link in its job's ``payload``, and
``url_display`` kept it too, its credential masked but its query whole.
Every member of a project reads its instances' jobs, payload included: a
presigned link's signature, or a share link's token, let any of them
download the archive. From this version the link is kept encrypted with
the credential (``secret_payload``, ``cloud.instance.restore_from_url``)
and the payload holds ``shown_link`` only. The jobs stored before are
changed alike: one that may still run — waiting, running, or failed and
so retried — keeps its link in ``secret_payload``; one that is done or
cancelled has no use for it and loses it, as the SaaS manager's pushes
lost their credentials in its 1.169.

``cloud.restore.upload.key`` arrives with this version: a key opens one
upload door ever, also after its grant is purged. The fingerprints of the
grants still stored are recorded in it.

Idempotent: a job without a link in its payload is left as it is, and a
fingerprint already recorded is skipped.
"""
import json
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.incubacloud.models.password_utils import decrypt_value
from odoo.addons.incubacloud.net.restore_source import shown_link

_logger = logging.getLogger(__name__)

#: States of a job that will not run again, nor be retried.
_ENDED = ("done", "cancelled")


def migrate(cr, version):
    """Move the links out of the restores' payloads; remember the door keys.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    moved = _move_restore_links(env)
    if moved:
        _logger.info("incubacloud 1.0.212: %d restore links left their payload", moved)
    _remember_door_keys(cr)


def _move_restore_links(env):
    """Take the link out of every restore job's payload.

    :param env: superuser environment
    :return: how many jobs held one
    :rtype: int
    """
    cr = env.cr
    cr.execute(
        "SELECT job.id, job.secret_payload FROM cloud_job job"
        " JOIN cloud_job_type type ON type.id = job.job_type_id"
        " WHERE type.code = 'restore_instance' AND job.payload LIKE %s",
        ('%"url"%',),
    )
    rows = cr.fetchall()
    moved = 0
    for job_id, cipher in rows:
        job = env["cloud.job"].browse(job_id)
        payload = dict(job.payload or {})
        url = payload.pop("url", None)
        if url is None:
            continue
        payload["url_display"] = shown_link(str(url))
        vals = {"payload": payload}
        if job.state not in _ENDED:
            vals["secret_payload"] = json.dumps(
                _readable_secret(cipher, env) | {"url": str(url)},
            )
        job.write(vals)
        moved += 1
    return moved


def _readable_secret(cipher, env):
    """Return what a job's ``secret_payload`` holds, as a dict.

    Read from its ciphertext rather than through the field, which opens
    an operator alert for a value that will not decrypt: one that does
    not holds nothing that can be used, and the link is kept alone.

    :param str cipher: the stored value, or None
    :param env: superuser environment
    :rtype: dict
    """
    if not cipher:
        return {}
    try:
        data = json.loads(decrypt_value(cipher, env) or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _remember_door_keys(cr):
    """Record the fingerprint of every grant still stored.

    :param cr: database cursor
    """
    cr.execute(
        "INSERT INTO cloud_restore_upload_key"
        " (fingerprint, create_uid, write_uid, create_date, write_date)"
        " SELECT DISTINCT fingerprint, %s, %s,"
        " now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC'"
        " FROM cloud_restore_upload_grant"
        " WHERE fingerprint IS NOT NULL AND fingerprint <> ''"
        " ON CONFLICT (fingerprint) DO NOTHING",
        (SUPERUSER_ID, SUPERUSER_ID),
    )
