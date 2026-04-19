"""
Post-migration for 1.0.1 — restore encrypted key_file values.

After Odoo has created the new key_file varchar column (EncryptedChar),
copy the pre-encrypted values from the backup column and remove it.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Check backup column exists (idempotency guard).
    cr.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'cloud_host' AND column_name = 'key_file_enc_bak'
    """)
    if not cr.fetchone():
        return

    cr.execute("""
        UPDATE cloud_host
        SET key_file = key_file_enc_bak
        WHERE key_file_enc_bak IS NOT NULL
    """)
    cr.execute("ALTER TABLE cloud_host DROP COLUMN key_file_enc_bak")
    _logger.info(
        "cloud_host.key_file post-migration complete: "
        "encrypted values restored to varchar column"
    )
