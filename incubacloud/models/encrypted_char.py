"""
EncryptedChar — a fields.Char subclass that transparently encrypts its value
before writing to the database and decrypts it when reading back into Python.

Usage:
    from .encrypted_char import EncryptedChar

    class MyModel(models.Model):
        secret = EncryptedChar(string="Secret")

The raw database column always stores the ``enc:<token>`` form.
Python code (executors, controllers) always sees plain text.
"""
from odoo import fields

from .password_utils import decrypt_value, encrypt_value


class EncryptedChar(fields.Char):
    """fields.Char that stores its value encrypted with Fernet."""

    # ── Write path ──────────────────────────────────────────────────────────

    def convert_to_write(self, value, record):
        value = super().convert_to_write(value, record)
        if not value:
            return value
        return encrypt_value(value, record.env)

    def convert_to_column(self, value, record, values=None, validate=True):
        value = super().convert_to_column(value, record, values=values, validate=validate)
        if not value:
            return value
        return encrypt_value(value, record.env)

    # ── Read path ───────────────────────────────────────────────────────────

    def convert_to_cache(self, value, record, validate=True):
        value = super().convert_to_cache(value, record, validate=validate)
        if not value:
            return value
        return decrypt_value(value, record.env)

    def convert_to_record(self, value, record):
        value = super().convert_to_record(value, record)
        if not value:
            return value
        return decrypt_value(value, record.env)
