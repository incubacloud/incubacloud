"""
Tier 1 — Pure-Python unit tests for password_utils.
No database required; run inside the Odoo test runner.

Classes inherit from ``odoo.tests.common.BaseCase`` rather than
``unittest.TestCase`` directly: BaseCase's ``__init_subclass__``
populates ``test_tags = {'standard', 'at_install'}`` and
``test_module``, which Odoo's tag selector requires. A plain
``unittest.TestCase`` is silently skipped under
``--test-tags /incubacloud``.
"""
import os
from unittest.mock import patch

from odoo.tests.common import BaseCase

import odoo.addons.incubacloud.models.password_utils as pw_mod


class TestGeneratePassword(BaseCase):

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


class TestIsEncrypted(BaseCase):

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


class TestEncryptDecrypt(BaseCase):

    def setUp(self):
        # Reset the lazy-loaded Fernet instance before every test
        pw_mod._fernet = None
        # The reset above is not enough on its own. ``patch.dict``
        # restores the environment variable when the block exits, but
        # the Fernet built from the throwaway key stays cached in the
        # module global — so every later reader in the same process
        # decrypts real ciphertext with a key that never wrote it. The
        # symptom is a CRITICAL naming a production row, which sends
        # you looking for corruption that is not there. Cost half an
        # investigation on 2026-08-29. Clearing it on the way out makes
        # the next reader re-initialise from the real environment.
        self.addCleanup(setattr, pw_mod, '_fernet', None)

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


class TestMultiFernetRotation(BaseCase):
    """Covers the comma-separated INCUBACLOUD_SECRET_KEY rotation path.

    Invariants under test:
      * Old ciphertext still decrypts when old key is trailing in the list.
      * New encryptions bind to the primary (first) key only — old key alone
        cannot decrypt them.
      * ``rotate_value`` produces a token decryptable by the new key alone,
        so operators can drop the old key after the rotation cron finishes.
    """

    def setUp(self):
        pw_mod._fernet = None
        # The reset above is not enough on its own. ``patch.dict``
        # restores the environment variable when the block exits, but
        # the Fernet built from the throwaway key stays cached in the
        # module global — so every later reader in the same process
        # decrypts real ciphertext with a key that never wrote it. The
        # symptom is a CRITICAL naming a production row, which sends
        # you looking for corruption that is not there. Cost half an
        # investigation on 2026-08-29. Clearing it on the way out makes
        # the next reader re-initialise from the real environment.
        self.addCleanup(setattr, pw_mod, '_fernet', None)

    def _fresh_key(self):
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def test_a_throwaway_key_never_outlives_its_test(self):
        """The leak this file used to have, pinned.

        Exercised through ``doCleanups`` — the same call the runner
        makes after every test method here — so the guarantee is
        asserted rather than assumed. Without the cleanup registered in
        ``setUp`` the module global would still be holding the
        throwaway key when the next test starts.
        """
        with patch.dict(
            os.environ, {"INCUBACLOUD_SECRET_KEY": self._fresh_key()},
        ):
            pw_mod._fernet = None
            pw_mod.encrypt_value("throwaway")
            self.assertIsNotNone(
                pw_mod._fernet, "the swapped key should be cached here",
            )
        self.doCleanups()
        self.assertIsNone(
            pw_mod._fernet,
            "a throwaway key must not survive the test that installed it",
        )

    def test_multifernet_decrypts_old_key_ciphertext(self):
        old_key = self._fresh_key()
        new_key = self._fresh_key()
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": old_key}):
            pw_mod._fernet = None
            encrypted = pw_mod.encrypt_value("legacy")
        with patch.dict(
            os.environ, {"INCUBACLOUD_SECRET_KEY": f"{new_key},{old_key}"},
        ):
            pw_mod._fernet = None
            self.assertEqual(pw_mod.decrypt_value(encrypted), "legacy")

    def test_multifernet_encrypts_with_primary_key(self):
        new_key = self._fresh_key()
        old_key = self._fresh_key()
        with patch.dict(
            os.environ, {"INCUBACLOUD_SECRET_KEY": f"{new_key},{old_key}"},
        ):
            pw_mod._fernet = None
            encrypted = pw_mod.encrypt_value("fresh")
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": new_key}):
            pw_mod._fernet = None
            self.assertEqual(pw_mod.decrypt_value(encrypted), "fresh")
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": old_key}):
            pw_mod._fernet = None
            with self.assertRaises(ValueError):
                pw_mod.decrypt_value(encrypted)

    def test_rotate_value_changes_ciphertext(self):
        old_key = self._fresh_key()
        new_key = self._fresh_key()
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": old_key}):
            pw_mod._fernet = None
            encrypted_old = pw_mod.encrypt_value("rotate-me")
        with patch.dict(
            os.environ, {"INCUBACLOUD_SECRET_KEY": f"{new_key},{old_key}"},
        ):
            pw_mod._fernet = None
            encrypted_new = pw_mod.rotate_value(encrypted_old)
        self.assertNotEqual(encrypted_old, encrypted_new)
        self.assertTrue(encrypted_new.startswith("enc:"))
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": new_key}):
            pw_mod._fernet = None
            self.assertEqual(pw_mod.decrypt_value(encrypted_new), "rotate-me")

    def test_rotate_value_empty_and_legacy_passthrough(self):
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": self._fresh_key()}):
            pw_mod._fernet = None
            self.assertEqual(pw_mod.rotate_value(""), "")
            self.assertIsNone(pw_mod.rotate_value(None))
            self.assertEqual(pw_mod.rotate_value("plain-legacy"), "plain-legacy")

    def test_rotate_value_no_key_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(pw_mod.IncubacloudCryptoError):
                pw_mod.rotate_value("enc:doesnotmatter==")

    def test_parse_keys_strips_and_filters_blanks(self):
        self.assertEqual(pw_mod._parse_keys("a,b,c"), [b"a", b"b", b"c"])
        self.assertEqual(pw_mod._parse_keys(" a , , b "), [b"a", b"b"])
        self.assertEqual(pw_mod._parse_keys(""), [])
