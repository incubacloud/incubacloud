"""Post-migrate for 1.0.82 — encrypt ``cloud.github.app.private_key``.

The field flipped from a plain ``fields.Text`` to ``EncryptedText``
(Fernet via ``INCUBACLOUD_SECRET_KEY``). Declaring the field encrypted
does nothing to rows that already exist: the column type is unchanged
(``text`` either way), so Odoo has no reason to touch it and the RSA
key would stay on disk in clear.

Raw SQL on both ends, for the reason spelled out in the sibling
migration that encrypted ``cloud.tenant.usage_token``: an ORM write
would short-circuit, because the cache holds the *plaintext* the read
path passed through unchanged, so the new value compares equal to the
old one and never reaches the column.

Idempotent — a value already carrying the ``enc:`` prefix is skipped,
which is also what makes this safe to re-run.

Hard-fails when ``INCUBACLOUD_SECRET_KEY`` is absent. Leaving the key
in plain text while reporting a successful upgrade is the one outcome
worth failing the update over.
"""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.incubacloud.models.password_utils import encrypt_value

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = 'enc:'


def migrate(cr, version):
    """Encrypt every stored GitHub App private key that is still plain."""
    env = api.Environment(cr, SUPERUSER_ID, {})

    cr.execute(
        "SELECT id, private_key FROM cloud_github_app "
        "WHERE private_key IS NOT NULL AND private_key <> ''"
    )
    rows = cr.fetchall()

    encrypted = 0
    skipped = 0
    for app_id, raw in rows:
        if raw.startswith(_ENCRYPTED_PREFIX):
            skipped += 1
            continue
        cr.execute(
            "UPDATE cloud_github_app SET private_key = %s WHERE id = %s",
            (encrypt_value(raw, env), app_id),
        )
        encrypted += 1

    if encrypted:
        env['cloud.github.app'].invalidate_model(['private_key'])

    _logger.info(
        "incubacloud: encrypted %d cloud.github.app private key(s), "
        "skipped %d already encrypted",
        encrypted, skipped,
    )
