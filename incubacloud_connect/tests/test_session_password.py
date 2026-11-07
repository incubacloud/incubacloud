"""The password a session ``/ic/login`` opens on 8.0 holds.

``models/res_users.py`` runs on 8.0 alone; what it signs and checks is
``models/session_password.py``, plain Python, tested here.
"""
import errno
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessDenied
from odoo.sql_db import Cursor
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


class TestOnlyItsOwnSessionPresentsIt(BaseCase):
    """``accepts``: what ``check_credentials`` takes, failing closed."""

    FINGERPRINT = "admin\x00\x00$pbkdf2$x"

    def setUp(self):
        super().setUp()
        self.password = sp.make(KEY, "prod", 7, self.FINGERPRINT, now=NOW)
        #: Every time the key or the fingerprint was asked for.
        self.loaded = []

    def _key(self):
        """The key, noting it was loaded."""
        self.loaded.append("key")
        return KEY

    def _fingerprint(self):
        """The row of uid 7, noting it was read."""
        self.loaded.append("fingerprint")
        return self.FINGERPRINT

    def _accepts(self, **changes):
        """``accepts`` for uid 7 on ``prod``, asked from its own session.

        :param changes: what it is asked with instead.
        :rtype: bool
        """
        asked = {
            "password": self.password, "db": "prod", "uid": 7,
            "session_uid": 7, "session_password": self.password,
            "key": self._key, "fingerprint": self._fingerprint,
            "now": NOW + 60,
        }
        asked.update(changes)
        return sp.accepts(**asked)

    def test_it_is_accepted_as_the_password_of_its_own_session(self):
        """Its user, its database, its session's own password."""
        self.assertTrue(self._accepts())

    def test_outside_its_session_it_is_refused_without_the_key(self):
        """XML-RPC or a cron (no session), another user's or another session."""
        other = sp.make(KEY, "prod", 7, self.FINGERPRINT, now=NOW)
        for changes in (
            {"session_uid": None, "session_password": None},
            {"session_uid": 8},
            {"session_password": other},
            {"session_password": "the user's own password"},
            {"session_password": None},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self._accepts(**changes))
        self.assertEqual(self.loaded, [])

    def test_a_password_that_is_not_text_is_refused_without_the_key(self):
        """What XML-RPC may carry besides text never reaches the key."""
        for other in (
            None, False, 0, 7, b"incubacloud-connect.1.2.3",
            ["incubacloud-connect.1.2.3"], {"incubacloud-connect.": 1},
        ):
            with self.subTest(other=other):
                self.assertFalse(
                    self._accepts(password=other, session_password=other),
                )
        self.assertEqual(self.loaded, [])

    def test_another_kind_of_password_never_loads_the_key(self):
        """A wrong password at the login page reads no key file."""
        self.assertFalse(
            self._accepts(password="admin", session_password="admin"),
        )
        self.assertEqual(self.loaded, [])

    def test_a_key_or_a_row_that_cannot_be_read_refuses_it(self):
        """Fails closed, and says why in the log."""
        def unreadable():
            """Fail as a key file Odoo may not read does."""
            raise OSError(errno.EACCES, "Permission denied")

        for changes in ({"key": unreadable}, {"fingerprint": unreadable}):
            with self.subTest(changes=changes), self.assertLogs(
                sp.__name__, "ERROR",
            ) as logged:
                self.assertFalse(self._accepts(**changes))
            self.assertIn("could not be checked", logged.output[0])

    def test_a_user_gone_refuses_it(self):
        """A user deleted or archived has no fingerprint."""
        self.assertFalse(self._accepts(fingerprint=lambda: None))


class TestOdoosCacheNeverKeepsIt(BaseCase):
    """``check``: 8.0 remembers a password checked once, in each worker."""

    def setUp(self):
        super().setUp()
        self.password = sp.make(KEY, "prod", 7, "fp", now=NOW)
        self.cursors = []
        #: The cursors credentials were checked with, and Odoo's checks.
        self.checked = []

    def _cursor(self):
        """A new cursor of the database."""
        cr = MagicMock(spec=Cursor)
        self.cursors.append(cr)
        return cr

    def _check(self, password, refuse=False):
        """``check`` *password*, noting what was asked.

        :param bool refuse: whether ``check_credentials`` refuses it.
        """
        def check_credentials(cr):
            """Note *cr*; refuse when asked to."""
            self.checked.append(cr)
            if refuse:
                raise AccessDenied()

        sp.check(
            password, self._cursor, check_credentials,
            lambda: self.checked.append("cached"),
        )

    def test_it_is_checked_again_on_every_call_with_a_cursor_of_its_own(self):
        """Never from what an earlier call accepted."""
        self._check(self.password)
        self._check(self.password)
        self.assertEqual(self.checked, self.cursors)
        self.assertEqual(len(self.cursors), 2)
        for cr in self.cursors:
            cr.close.assert_called_once_with()

    def test_refused_it_raises_and_its_cursor_closes(self):
        """``AccessDenied`` reaches the caller; the cursor goes anyway."""
        with self.assertRaises(AccessDenied):
            self._check(self.password, refuse=True)
        self.cursors[0].close.assert_called_once_with()

    def test_any_other_password_goes_to_odoo_s_own_check(self):
        """Opening no cursor of its own."""
        for other in ("admin", "", None):
            self._check(other)
        self.assertEqual(self.checked, ["cached"] * 3)
        self.assertEqual(self.cursors, [])


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

    def test_it_reaches_the_disk_before_it_is_linked(self):
        """A crash after the link leaves the key, not an empty file."""
        done = []
        fsync, link = os.fsync, os.link

        def synced(fd):
            """Note the sync, then sync."""
            done.append("fsync")
            fsync(fd)

        def linked(draft, path):
            """Note the link, then link."""
            done.append("link")
            link(draft, path)

        with patch.object(sp.os, "fsync", side_effect=synced), patch.object(
            sp.os, "link", side_effect=linked,
        ):
            sp.load_key(self.path)
        self.assertEqual(done, ["fsync", "link"])

    def test_drafts_an_earlier_attempt_left_go_when_it_is_created(self):
        """Not anything else of the data directory."""
        for name in ("incubacloud_connect.key.tmpab12cd", "tmp_other", "sessions"):
            Path(self.folder, name).write_bytes(b"left")
        sp.load_key(self.path)
        self.assertEqual(
            sorted(os.listdir(self.folder)),
            ["incubacloud_connect.key", "sessions", "tmp_other"],
        )

    def test_a_worker_whose_draft_the_winner_removed_reads_the_winner_s(self):
        """The winner removes the drafts left, this worker's among them."""
        def link_after_cleanup(draft, path):
            """Another worker links its key and removes *draft* first."""
            Path(path).write_bytes(b"the winner's key")
            Path(draft).unlink()
            raise OSError(errno.ENOENT, "No such file or directory")

        with patch.object(sp.os, "link", side_effect=link_after_cleanup):
            self.assertEqual(sp.load_key(self.path), b"the winner's key")
        self.assertEqual(os.listdir(self.folder), ["incubacloud_connect.key"])

    def test_an_empty_key_file_is_refused(self):
        Path(self.path).write_bytes(b"")
        with self.assertRaises(ValueError):
            sp.load_key(self.path)
