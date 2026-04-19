"""
Pre-migration for 1.0.1 — convert cloud_host.key_file from bytea (Binary) to
the encrypted varchar format used by EncryptedChar.

Steps:
  1. Read every existing key_file value (raw bytes from the bytea column).
  2. Decode the bytes as UTF-8 to recover the original PEM text.
  3. Encrypt the PEM text with Fernet via encrypt_value().
  4. Persist the encrypted string in a temporary TEXT column key_file_enc_bak.
  5. Drop the original key_file bytea column so Odoo can recreate it as
     varchar when it applies the new EncryptedChar field definition.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        # Fresh install — no data to migrate.
        return

    # Check if column exists and is still bytea (idempotency guard).
    cr.execute("""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_name = 'cloud_host' AND column_name = 'key_file'
    """)
    row = cr.fetchone()
    if not row:
        _logger.info("cloud_host.key_file column not found — skipping migration")
        return
    if row[0] != 'bytea':
        _logger.info(
            "cloud_host.key_file is already %s — skipping pre-migration", row[0]
        )
        return

    # Import here so we get the key resolved from INCUBACLOUD_SECRET_KEY env.
    from odoo.addons.incubacloud.models.password_utils import encrypt_value

    # Create the backup column.
    cr.execute(
        "ALTER TABLE cloud_host"
        " ADD COLUMN IF NOT EXISTS key_file_enc_bak TEXT"
    )

    # Read existing PEM keys and encrypt them.
    cr.execute(
        "SELECT id, key_file FROM cloud_host WHERE key_file IS NOT NULL"
    )
    rows = cr.fetchall()
    migrated = 0
    for host_id, key_bytes in rows:
        if not key_bytes:
            continue
        try:
            pem = bytes(key_bytes).decode('utf-8', errors='replace')
            encrypted = encrypt_value(pem)
            cr.execute(
                "UPDATE cloud_host SET key_file_enc_bak = %s WHERE id = %s",
                (encrypted, host_id),
            )
            migrated += 1
        except Exception:
            _logger.exception(
                "Failed to migrate key_file for cloud.host id=%s — "
                "key will be lost after migration",
                host_id,
            )

    # Drop the bytea column so Odoo recreates it as varchar (EncryptedChar).
    cr.execute("ALTER TABLE cloud_host DROP COLUMN key_file")
    _logger.info(
        "cloud_host.key_file pre-migration complete: %d key(s) encrypted "
        "and backed up; original bytea column dropped",
        migrated,
    )
