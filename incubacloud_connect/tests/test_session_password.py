"""The password a session ``/ic/login`` opens on 8.0 holds.

``models/res_users.py`` runs on 8.0 alone; what it signs and checks is
``models/session_password.py``, plain Python, tested here.
"""
import errno
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_connect.models import session_password as sp

KEY = b"0123456789abcdef" * 4
NOW = 1_800_000_000


class TestTheSessionPassword(BaseCase):
    """Accepted for the session it was made for, and for nothing else."""

    def _made(self, **changes):
        """A password made at ``NOW`` for uid 7 of database ``prod``.

        :param changes: what ``valid`` is asked with instead.
        :returns: what ``valid`` answers.
        :rtype: bool
        """
        password = sp.make(KEY, "prod", 7, "admin\x00\x00$pbkdf2$x", now=NOW)
        asked = {
            "key": KEY, "db": "prod", "uid": 7,
            "fingerprint": "admin\x00\x00$pbkdf2$x", "password": password,
            "now": NOW + 60,
        }
        asked.update(changes)
        return sp.valid(**asked)

    def test_it_is_accepted_for_its_user_on_its_database(self):
        self.assertTrue(self._made())

    def test_another_user_database_or_key_refuses_it(self):
        self.assertFalse(self._made(uid=8))
        self.assertFalse(self._made(db="other"))
        self.assertFalse(self._made(key=b"another key"))

    def test_a_changed_password_login_or_hash_ends_it(self):
        self.assertFalse(self._made(fingerprint="admin\x00\x00$pbkdf2$y"))
        self.assertFalse(self._made(fingerprint="root\x00\x00$pbkdf2$x"))

    def test_it_ends_after_a_week(self):
        self.assertTrue(self._made(now=NOW + sp.LIFETIME - 1))
        self.assertFalse(self._made(now=NOW + sp.LIFETIME + 1))

    def test_a_password_altered_or_of_another_shape_is_refused(self):
        password = sp.make(KEY, "prod", 7, "fp", now=NOW)
        expires, nonce, mac = password[len(sp.PREFIX):].split(".")
        for other in (
            f"{sp.PREFIX}{int(expires) + 3600}.{nonce}.{mac}",
            f"{sp.PREFIX}{expires}.{'0' * 32}.{mac}",
            f"{sp.PREFIX}{expires}.{nonce}.{'0' * 64}",
            f"{sp.PREFIX}{expires}.{nonce}",
            f"{sp.PREFIX}soon.{nonce}.{mac}",
            password[len(sp.PREFIX):], "admin", "", None, False,
        ):
            with self.subTest(other=other):
                self.assertFalse(
                    sp.valid(KEY, "prod", 7, "fp", other, now=NOW + 60),
                )

    def test_two_passwords_of_one_user_differ(self):
        self.assertNotEqual(
            sp.make(KEY, "prod", 7, "fp", now=NOW),
            sp.make(KEY, "prod", 7, "fp", now=NOW),
        )

    def test_a_login_that_is_not_ascii_signs(self):
        password = sp.make(KEY, "prod", 7, "josé\x00\x00h", now=NOW)
        self.assertTrue(sp.valid(KEY, "prod", 7, "josé\x00\x00h", password, now=NOW))


class TestTheKey(BaseCase):
    """Created once, owner-only, and the same for every worker."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.path = os.path.join(self.folder, "incubacloud_connect.key")

    def test_the_first_read_creates_it_owner_only(self):
        key = sp.load_key(self.path)
        self.assertEqual(len(key), 64)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(sp.load_key(self.path), key)
        self.assertEqual(os.listdir(self.folder), ["incubacloud_connect.key"])

    def test_a_key_already_there_is_kept(self):
        Path(self.path).write_bytes(b"the key another worker wrote")
        self.assertEqual(sp.load_key(self.path), b"the key another worker wrote")

    def test_a_worker_that_loses_the_race_reads_the_winner_s(self):
        """Its own draft goes; the key is the one linked first."""
        def link_after_another(_draft, path):
            Path(path).write_bytes(b"the winner's key")
            raise OSError(errno.EEXIST, "File exists")

        with patch.object(sp.os, "link", side_effect=link_after_another):
            self.assertEqual(sp.load_key(self.path), b"the winner's key")
        self.assertEqual(os.listdir(self.folder), ["incubacloud_connect.key"])

    def test_an_empty_key_file_is_refused(self):
        Path(self.path).write_bytes(b"")
        with self.assertRaises(ValueError):
            sp.load_key(self.path)
