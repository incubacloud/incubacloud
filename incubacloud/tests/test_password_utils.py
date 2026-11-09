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
import logging
import os
import tempfile
import threading
from unittest.mock import patch

from odoo.tests.common import BaseCase
from odoo.tools import config

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


class TestSecretKeySources(BaseCase):
    """Where the key chain is read from, in order, the first that exists.

    1. ``INCUBACLOUD_SECRET_KEY`` in the environment;
    2. ``INCUBACLOUD_SECRET_KEY_FILE``, a file holding the chain;
    3. ``secret_key_file`` in the ``[incubacloud]`` section of odoo.conf;
    4. ``secret_key`` in that same section.

    A source that exists but holds nothing usable fails loudly rather
    than falling through to the next one, and no log line ever carries
    a key. Each test writes its own odoo.conf and key files in a
    temporary directory and points the module at that odoo.conf, so the
    configuration of the server running the tests is never read.
    """

    _LOGGER = "odoo.addons.incubacloud.models.password_utils"

    def setUp(self):
        pw_mod._fernet = None
        pw_mod._key_failure = None
        self.addCleanup(setattr, pw_mod, "_fernet", None)
        self.addCleanup(setattr, pw_mod, "_key_failure", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.conf = os.path.join(self.tmp, "odoo.conf")
        self._write(self.conf, "[options]\nworkers = 0\n")
        conf_patch = patch.object(
            pw_mod, "_odoo_conf_path", autospec=True, return_value=self.conf,
        )
        conf_patch.start()
        self.addCleanup(conf_patch.stop)

    def _fresh_key(self):
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def _write(self, path, content):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def _conf(self, **options):
        """Write odoo.conf with an ``[incubacloud]`` section holding *options*."""
        lines = ["[options]", "workers = 0", "", "[incubacloud]"]
        lines += [f"{name} = {value}" for name, value in options.items()]
        self._write(self.conf, "\n".join(lines) + "\n")

    def _key_file(self, content, name="key", mode=0o600):
        """Write a key file owner-only (or with *mode*) and return its path."""
        path = self._write(os.path.join(self.tmp, name), content)
        os.chmod(path, mode)
        return path

    def _encrypt_with(self, key):
        """Return ``"secret"`` encrypted with *key* alone."""
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": key}, clear=True):
            pw_mod._fernet = None
            value = pw_mod.encrypt_value("secret")
        pw_mod._fernet = None
        return value

    def _assert_reads_with(self, key):
        """The chain now loaded decrypts what *key* alone encrypted."""
        encrypted = self._encrypt_with(key)
        self.assertEqual(pw_mod.decrypt_value(encrypted), "secret")

    # ── each source, and the order ──────────────────────────────────────────

    def test_environment_comes_first(self):
        env_key, file_key, conf_key = (self._fresh_key() for _ in range(3))
        self._conf(secret_key=conf_key)
        env = {
            "INCUBACLOUD_SECRET_KEY": env_key,
            "INCUBACLOUD_SECRET_KEY_FILE": self._key_file(file_key),
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(pw_mod._read_secret_key()[1], env_key)
            self._assert_reads_with(env_key)

    def test_key_file_from_the_environment_comes_second(self):
        file_key, conf_file_key, conf_key = (self._fresh_key() for _ in range(3))
        self._conf(
            secret_key_file=self._key_file(conf_file_key, "conf-key"),
            secret_key=conf_key,
        )
        env = {"INCUBACLOUD_SECRET_KEY_FILE": self._key_file(file_key + "\n")}
        with patch.dict(os.environ, env, clear=True):
            source, raw = pw_mod._read_secret_key()
            self.assertIn("INCUBACLOUD_SECRET_KEY_FILE", source)
            self.assertEqual(raw, file_key)
            self._assert_reads_with(file_key)

    def test_odoo_conf_key_file_comes_third(self):
        conf_file_key, conf_key = self._fresh_key(), self._fresh_key()
        self._conf(
            secret_key_file=self._key_file(conf_file_key),
            secret_key=conf_key,
        )
        with patch.dict(os.environ, {}, clear=True):
            source, raw = pw_mod._read_secret_key()
            self.assertIn("secret_key_file", source)
            self.assertEqual(raw, conf_file_key)
            self._assert_reads_with(conf_file_key)

    def test_odoo_conf_key_comes_last(self):
        conf_key = self._fresh_key()
        self._conf(secret_key=conf_key)
        with patch.dict(os.environ, {}, clear=True):
            source, raw = pw_mod._read_secret_key()
            self.assertIn("[incubacloud] secret_key", source)
            self.assertIn(self.conf, source)
            self.assertEqual(raw, conf_key)
            self._assert_reads_with(conf_key)

    def test_no_source_at_all(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pw_mod._read_secret_key(), (None, ""))
            with self.assertLogs(self._LOGGER, "ERROR"):
                self.assertFalse(pw_mod.key_is_configured())

    def test_odoo_conf_without_the_section_is_no_source(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pw_mod._read_secret_key(), (None, ""))

    def test_the_error_names_every_source(self):
        message = pw_mod._key_error_message()
        for name in (
            "INCUBACLOUD_SECRET_KEY", "INCUBACLOUD_SECRET_KEY_FILE",
            "secret_key_file", "secret_key", "[incubacloud]",
        ):
            self.assertIn(name, message)

    # ── rotation ────────────────────────────────────────────────────────────

    def _assert_rotation(self):
        """The loaded chain is ``new,old``: both read, only new encrypts."""
        encrypted = pw_mod.encrypt_value("fresh")
        self.assertEqual(pw_mod.decrypt_value(self._old_value), "secret")
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": self._new}, clear=True):
            pw_mod._fernet = None
            self.assertEqual(pw_mod.decrypt_value(encrypted), "fresh")

    def _rotation_keys(self):
        self._new, old = self._fresh_key(), self._fresh_key()
        self._old_value = self._encrypt_with(old)
        return f"{self._new}, {old}"

    def test_rotation_from_a_key_file(self):
        env = {"INCUBACLOUD_SECRET_KEY_FILE": self._key_file(self._rotation_keys())}
        with patch.dict(os.environ, env, clear=True):
            self._assert_rotation()

    def test_rotation_from_odoo_conf(self):
        self._conf(secret_key=self._rotation_keys())
        with patch.dict(os.environ, {}, clear=True):
            self._assert_rotation()

    def test_rotation_from_odoo_conf_key_file(self):
        self._conf(secret_key_file=self._key_file(self._rotation_keys()))
        with patch.dict(os.environ, {}, clear=True):
            self._assert_rotation()

    # ── a source that exists but is wrong fails, it does not fall through ──

    def _assert_fails_naming(self, *names):
        with self.assertLogs(self._LOGGER, "ERROR") as logs:
            self.assertIsNone(pw_mod._get_fernet())
        output = "\n".join(logs.output)
        for name in names:
            self.assertIn(name, output)
        with self.assertRaises(pw_mod.IncubacloudCryptoError):
            pw_mod.encrypt_value("secret")
        return output

    def test_unreadable_key_file_fails(self):
        self._conf(secret_key=self._fresh_key())
        missing = os.path.join(self.tmp, "nowhere")
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY_FILE": missing}, clear=True):
            self._assert_fails_naming("INCUBACLOUD_SECRET_KEY_FILE", missing)

    def test_a_directory_as_key_file_fails(self):
        self._conf(secret_key_file=self.tmp, secret_key=self._fresh_key())
        with patch.dict(os.environ, {}, clear=True):
            self._assert_fails_naming("secret_key_file", self.tmp)

    def test_empty_key_file_fails(self):
        env = {"INCUBACLOUD_SECRET_KEY_FILE": self._key_file("  \n")}
        self._conf(secret_key=self._fresh_key())
        with patch.dict(os.environ, env, clear=True):
            self._assert_fails_naming("INCUBACLOUD_SECRET_KEY_FILE", "empty")

    def test_empty_environment_variable_fails(self):
        self._conf(secret_key=self._fresh_key())
        with patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY": ""}, clear=True):
            self._assert_fails_naming("INCUBACLOUD_SECRET_KEY", "empty")

    def test_empty_odoo_conf_key_fails(self):
        self._conf(secret_key="")
        with patch.dict(os.environ, {}, clear=True):
            self._assert_fails_naming("[incubacloud] secret_key", "empty")

    def test_malformed_odoo_conf_key_fails_without_logging_it(self):
        bad = "not-a-fernet-key-but-a-secret-all-the-same"
        self._conf(secret_key=bad)
        with patch.dict(os.environ, {}, clear=True):
            output = self._assert_fails_naming("[incubacloud] secret_key")
        self.assertNotIn(bad, output)

    def test_unparseable_odoo_conf_fails(self):
        self._write(self.conf, "[incubacloud]\nsecret_key = a\nsecret_key = b\n")
        with patch.dict(os.environ, {}, clear=True):
            self._assert_fails_naming(self.conf)

    # ── the log says where, never what ──────────────────────────────────────

    def test_the_log_names_the_source_never_the_key(self):
        new, old = self._fresh_key(), self._fresh_key()
        self._conf(secret_key=f"{new},{old}")
        with (
            patch.dict(os.environ, {}, clear=True),
            # The level as a number: Odoo names its level 25 "INFO" too,
            # and assertLogs would read the name as that one.
            self.assertLogs(self._LOGGER, logging.INFO) as logs,
        ):
            self.assertTrue(pw_mod.key_is_configured())
        output = "\n".join(logs.output)
        self.assertIn("[incubacloud] secret_key", output)
        self.assertNotIn(new, output)
        self.assertNotIn(old, output)

    # ── the key file is checked before it is read ───────────────────────────

    def _read_with_deadline(self, path, seconds=5):
        """Run ``_read_key_file`` on a thread; fail when it does not return.

        A FIFO opened for reading blocks until someone writes to it: the
        read must refuse it instead, and a test that hangs would take the
        whole run with it.

        :return: the exception it raised, or ``None``
        """
        outcome = []

        def run():
            try:
                pw_mod._read_key_file(path, "the test file")
            except pw_mod.IncubacloudCryptoError as e:
                outcome.append(e)
            else:
                outcome.append(None)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(seconds)
        self.assertFalse(worker.is_alive(), "reading the file blocked")
        return outcome[0]

    def test_a_fifo_as_key_file_is_refused_without_blocking(self):
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo)
        error = self._read_with_deadline(fifo)
        self.assertIn("regular file", str(error))

    def test_a_device_as_key_file_is_refused(self):
        error = self._read_with_deadline("/dev/null")
        self.assertIn("regular file", str(error))

    def test_a_key_file_over_64_kib_is_refused(self):
        big = self._key_file("k" * (64 * 1024 + 1), "big")
        error = self._read_with_deadline(big)
        self.assertIn("64 KiB", str(error))

    def test_a_key_file_of_exactly_64_kib_is_read(self):
        path = self._key_file("k" * (64 * 1024), "limit")
        self.assertEqual(
            len(pw_mod._read_key_file(path, "the test file")), 64 * 1024,
        )

    def test_a_relative_key_file_path_asks_for_an_absolute_one(self):
        env = {"INCUBACLOUD_SECRET_KEY_FILE": "key"}
        with patch.dict(os.environ, env, clear=True):
            output = self._assert_fails_naming("INCUBACLOUD_SECRET_KEY_FILE")
        self.assertIn("absolute path", output)

    def test_a_key_file_readable_by_everyone_warns(self):
        key = self._fresh_key()
        path = self._key_file(key, "open", mode=0o644)
        with (
            patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY_FILE": path}, clear=True),
            self.assertLogs(self._LOGGER, logging.WARNING) as logs,
        ):
            self.assertTrue(pw_mod.key_is_configured())
        output = "\n".join(logs.output)
        self.assertIn("readable by every user", output)
        self.assertIn(path, output)
        self.assertNotIn(key, output)

    def test_a_private_key_file_does_not_warn(self):
        path = self._key_file(self._fresh_key(), "private", mode=0o600)
        with (
            patch.dict(os.environ, {"INCUBACLOUD_SECRET_KEY_FILE": path}, clear=True),
            self.assertNoLogs(self._LOGGER, logging.WARNING),
        ):
            self.assertTrue(pw_mod.key_is_configured())

    # ── a missing key is looked for again after a while, not on every call ──

    def _fail_to_read(self, clock):
        """Make the key missing everywhere and count the reads of the sources.

        :param clock: the mocked ``time`` module of ``password_utils``
        :return: the mock standing for ``_read_secret_key``
        """
        clock.monotonic.return_value = 1000.0
        reader = patch.object(
            pw_mod, "_read_secret_key", autospec=True,
            side_effect=pw_mod.IncubacloudCryptoError("Cannot read the key."),
        )
        mocked = reader.start()
        self.addCleanup(reader.stop)
        return mocked

    def test_a_missing_key_is_logged_and_read_once_per_interval(self):
        with (
            patch.object(pw_mod, "time", autospec=True) as clock,
            patch.dict(os.environ, {}, clear=True),
        ):
            reader = self._fail_to_read(clock)
            with self.assertLogs(self._LOGGER, "ERROR") as logs:
                for _ in range(5):
                    self.assertIsNone(pw_mod._get_fernet())
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(reader.call_count, 1)

    def test_a_missing_key_is_looked_for_again_after_the_interval(self):
        with (
            patch.object(pw_mod, "time", autospec=True) as clock,
            patch.dict(os.environ, {}, clear=True),
        ):
            reader = self._fail_to_read(clock)
            with self.assertLogs(self._LOGGER, "ERROR") as logs:
                pw_mod._get_fernet()
                clock.monotonic.return_value += pw_mod._KEY_RETRY_SECONDS - 1
                pw_mod._get_fernet()
                self.assertEqual(reader.call_count, 1)
                clock.monotonic.return_value += 2
                pw_mod._get_fernet()
            self.assertEqual(reader.call_count, 2)
            self.assertEqual(len(logs.output), 2)

    def test_a_key_that_appears_after_the_wait_is_used(self):
        key = self._fresh_key()
        with (
            patch.object(pw_mod, "time", autospec=True) as clock,
            patch.dict(os.environ, {}, clear=True),
        ):
            clock.monotonic.return_value = 1000.0
            with self.assertLogs(self._LOGGER, "ERROR"):
                self.assertIsNone(pw_mod._get_fernet())
            self._conf(secret_key=key)
            clock.monotonic.return_value += pw_mod._KEY_RETRY_SECONDS + 1
            self.assertIsNotNone(pw_mod._get_fernet())

    def test_another_environment_is_not_held_back_by_an_old_failure(self):
        """The wait is for the same configuration only.

        A process whose environment changes (the tests') must not find a
        valid key refused for a failure recorded with a different one.
        """
        with (
            patch.object(pw_mod, "time", autospec=True) as clock,
            patch.dict(os.environ, {}, clear=True),
        ):
            clock.monotonic.return_value = 1000.0
            with self.assertLogs(self._LOGGER, "ERROR"):
                self.assertIsNone(pw_mod._get_fernet())
        env = {"INCUBACLOUD_SECRET_KEY": self._fresh_key()}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNotNone(pw_mod._get_fernet())

    def test_the_encrypt_error_is_still_raised_while_waiting(self):
        with (
            patch.object(pw_mod, "time", autospec=True) as clock,
            patch.dict(os.environ, {}, clear=True),
        ):
            self._fail_to_read(clock)
            for _ in range(2):
                with self.assertRaises(pw_mod.IncubacloudCryptoError):
                    pw_mod.encrypt_value("secret")

    # ── odoo.conf ───────────────────────────────────────────────────────────

    def test_a_parse_error_does_not_log_the_line_that_holds_the_key(self):
        """The parser quotes the offending line: here, one before any section."""
        key = self._fresh_key()
        self._write(self.conf, f"secret_key = {key}\n")
        with patch.dict(os.environ, {}, clear=True):
            output = self._assert_fails_naming(self.conf)
            with self.assertRaises(pw_mod.IncubacloudCryptoError) as caught:
                pw_mod._read_conf_section(self.conf)
        self.assertNotIn(key, output)
        self.assertNotIn(key, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)


class TestOdooConfPath(BaseCase):
    """The path of odoo.conf comes from Odoo's own option, read for real.

    Odoo 19 keeps it in ``config["config"]``; the older ``rcfile``
    attribute is deprecated. The function is not mocked here: a mock
    would hide a rename of the option.
    """

    def test_it_is_the_option_odoo_was_started_with(self):
        self.assertEqual(pw_mod._odoo_conf_path(), config.get("config"))
        self.assertIsInstance(pw_mod._odoo_conf_path(), str)

    def test_it_follows_a_change_of_the_option(self):
        previous = config["config"]
        self.addCleanup(config.__setitem__, "config", previous)
        config["config"] = "/etc/odoo/elsewhere.conf"
        self.assertEqual(pw_mod._odoo_conf_path(), "/etc/odoo/elsewhere.conf")
