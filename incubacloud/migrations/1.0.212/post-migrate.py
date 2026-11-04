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

A restore that took a safety backup first kept its credential on the
chain's first job, the ``backup_create``: that job ended long ago with
the credential still in its ``secret_payload``, and the restore's own
step never had it — a retry of a failed one could not log in. The
credential moves to the restore's step, or to the latest retry of it,
when that one may still run; otherwise it is only taken off the backup
(``_move_chain_credentials``).

``cloud.restore.upload.key`` arrives with this version: a key opens one
upload door ever, also after its grant is purged. The fingerprints of the
grants still stored are recorded in it.

Idempotent: a job without a link in its payload is left as it is, a
backup without a credential too, and a fingerprint already recorded is
skipped.
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
    credentials = _move_chain_credentials(env)
    if credentials:
        _logger.info(
            "incubacloud 1.0.212: %d restore credentials left their safety backup",
            credentials,
        )
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


#: The keys of a restore's credential (``restore_from_url`` before 1.0.212).
_CREDENTIAL_KEYS = ("machine", "login", "password")


def _move_chain_credentials(env):
    """Move each restore's credential off the safety backup that kept it.

    Before 1.0.212 a restore from a link with a safety backup first
    wrote its credential on the chain's first job, a ``backup_create``.
    Its restore step — or the latest retry of it, which carries no chain
    key — gets the credential when it may still run (not done nor
    cancelled); either way the backup keeps none of it.

    :param env: superuser environment
    :return: how many backups held one
    :rtype: int
    """
    cr = env.cr
    # What ``_move_restore_links`` wrote, read back below as stored.
    env.flush_all()
    cr.execute(
        "SELECT job.id, job.secret_payload, job.queue_chain_key FROM cloud_job job"
        " JOIN cloud_job_type type ON type.id = job.job_type_id"
        " WHERE type.code = 'backup_create' AND job.queue_chain_key IS NOT NULL"
        " AND job.secret_payload IS NOT NULL AND job.secret_payload <> ''",
    )
    Job = env["cloud.job"]
    moved = 0
    for job_id, cipher, chain_key in cr.fetchall():
        secret = _readable_secret(cipher, env)
        credential = {
            key: secret[key] for key in _CREDENTIAL_KEYS if key in secret
        }
        if not credential.get("login"):
            continue
        restores = Job.search([
            ("queue_chain_key", "=", chain_key),
            ("job_type_id.code", "=", "restore_instance"),
        ])
        restores = restores.filtered(
            lambda job: (job.payload or {}).get("mode") == "from_url",
        )
        if not restores:
            continue
        for restore in restores:
            latest = _latest_retry(restore)
            if latest.state not in _ENDED:
                latest.secret_payload = json.dumps(
                    _readable_secret(
                        _stored_secret(cr, latest.id), env,
                    ) | credential,
                )
                latest.flush_recordset(["secret_payload"])
        rest = {key: value for key, value in secret.items() if key not in credential}
        Job.browse(job_id).secret_payload = json.dumps(rest) if rest else False
        moved += 1
    return moved


def _latest_retry(job):
    """Return the last retry of *job*, or *job* when it has none.

    :param job: a ``cloud.job``
    :rtype: cloud.job
    """
    Job = job.env["cloud.job"]
    seen = {job.id}
    while True:
        retry = Job.search([("retry_of_id", "=", job.id)], order="id desc", limit=1)
        if not retry or retry.id in seen:
            return job
        seen.add(retry.id)
        job = retry


def _stored_secret(cr, job_id):
    """Return the stored ciphertext of a job's ``secret_payload``.

    :param cr: database cursor
    :param int job_id: the job
    :rtype: str
    """
    cr.execute("SELECT secret_payload FROM cloud_job WHERE id = %s", (job_id,))
    row = cr.fetchone()
    return row[0] if row else None


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
