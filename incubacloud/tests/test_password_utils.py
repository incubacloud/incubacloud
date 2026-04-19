"""
Tier 1 — Pure-Python unit tests for password_utils.
No database required; run inside the Odoo test runner.
"""
import os
import unittest
from unittest.mock import patch

import odoo.addons.incubacloud.models.password_utils as pw_mod


class TestGeneratePassword(unittest.TestCase):

    def test_returns_string(self):
        self.assertIsInstance(pw_mod.generate_password(), str)

    def test_min_length(self):
        # secrets.token_urlsafe(20) → ~27 URL-safe chars
        self.assertGreaterEqual(len(pw_mod.generate_password()), 10)

    def test_custom_length_longer(self):
        short = pw_mod.generate_password(10)
        long_ = pw_mod.generate_password(40)
        self.assertGreater(len(long_), len(short))

    def test_unique_per_call(self):
        passwords = {pw_mod.generate_password() for _ in range(10)}
        self.assertEqual(len(passwords), 10)


class TestIsEncrypted(unittest.TestCase):

    def test_enc_prefix_true(self):
        self.assertTrue(pw_mod.is_encrypted("enc:sometoken=="))

    def test_plain_false(self):
        self.assertFalse(pw_mod.is_encrypted("plainpassword"))

    def test_none_false(self):
        self.assertFalse(pw_mod.is_encrypted(None))

    def test_empty_false(self):
        self.assertFalse(pw_mod.is_encrypted(""))

    def test_enc_without_colon_false(self):
        self.assertFalse(pw_mod.is_encrypted("enctoken"))


class TestEncryptDecrypt(unittest.TestCase):

    def setUp(self):
        # Reset the lazy-loaded Fernet instance before every test
        pw_mod._fernet = None

    def _fresh_key(self):
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    # ── encrypt_value ────────────────────────────────────────────────────────

    def test_encrypt_produces_enc_prefix(self):
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": self._fresh_key()}):
            result = pw_mod.encrypt_value("hello")
        self.assertTrue(result.startswith("enc:"))

    def test_encrypt_empty_returns_empty(self):
        self.assertEqual(pw_mod.encrypt_value(""), "")

    def test_encrypt_none_returns_none(self):
        self.assertIsNone(pw_mod.encrypt_value(None))

    def test_encrypt_already_encrypted_is_noop(self):
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": self._fresh_key()}):
            first = pw_mod.encrypt_value("secret")
            pw_mod._fernet = None  # reset to force re-init with same key via patch
            second = pw_mod.encrypt_value(first)
        self.assertEqual(first, second)

    def test_encrypt_no_key_raises(self):
        """When no key is available encrypt_value must raise (fail-loud)."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(pw_mod.IncubacloudCryptoError):
                pw_mod.encrypt_value("mypassword")

    def test_decrypt_no_key_raises_for_encrypted(self):
        """decrypt_value raises when key is missing and value was encrypted."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(pw_mod.IncubacloudCryptoError):
                pw_mod.decrypt_value("enc:somebase64==")

    def test_decrypt_no_key_passes_through_legacy_plain(self):
        """Plain-text legacy values pass through even without a key."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pw_mod.decrypt_value("plain"), "plain")

    def test_decrypt_bad_token_raises_valueerror(self):
        """Corrupt ciphertext must raise ValueError, not return None."""
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": self._fresh_key()}):
            pw_mod._fernet = None
            with self.assertRaises(ValueError):
                pw_mod.decrypt_value("enc:not-a-real-token")

    # ── decrypt_value ────────────────────────────────────────────────────────

    def test_decrypt_plain_unchanged(self):
        self.assertEqual(pw_mod.decrypt_value("plain"), "plain")

    def test_decrypt_empty_returns_empty(self):
        self.assertEqual(pw_mod.decrypt_value(""), "")

    def test_decrypt_none_returns_none(self):
        self.assertIsNone(pw_mod.decrypt_value(None))

    # ── round-trip ───────────────────────────────────────────────────────────

    def test_roundtrip_ascii(self):
        key = self._fresh_key()
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": key}):
            pw_mod._fernet = None
            encrypted = pw_mod.encrypt_value("supersecret!")
            self.assertTrue(pw_mod.is_encrypted(encrypted))
            decrypted = pw_mod.decrypt_value(encrypted)
        self.assertEqual(decrypted, "supersecret!")

    def test_roundtrip_special_chars(self):
        key = self._fresh_key()
        value = "p@$$w0rd!#%^&*()_+-=[]{}|;':\",./<>?"
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": key}):
            pw_mod._fernet = None
            encrypted = pw_mod.encrypt_value(value)
            decrypted = pw_mod.decrypt_value(encrypted)
        self.assertEqual(decrypted, value)

    def test_roundtrip_unicode(self):
        key = self._fresh_key()
        value = "contraseña-ñoño-АБВ"
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": key}):
            pw_mod._fernet = None
            encrypted = pw_mod.encrypt_value(value)
            decrypted = pw_mod.decrypt_value(encrypted)
        self.assertEqual(decrypted, value)
