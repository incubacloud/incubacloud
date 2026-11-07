"""Core's code locks rows softly, and the checker can tell.

The rule and why it exists are in ``row_lock_rules``. The second class
matters as much as the first: a checker that never fires passes every
repository, so it is fed code that breaks the rule on purpose.
"""
import textwrap
from pathlib import Path

from odoo.tests.common import BaseCase

from .row_lock_rules import addon_files, check, check_sources

#: The root of core's repository: every addon in it is scanned.
_REPO = Path(__file__).resolve().parents[2]


class TestCoreLocksRowsSoftly(BaseCase):

    def test_there_is_code_to_check(self):
        """The scan reaches core's addons, so its silence means something."""
        names = {p.relative_to(_REPO).as_posix() for p in addon_files(_REPO)}
        self.assertIn("incubacloud/models/cloud_instance_metrics.py", names)
        self.assertIn("incubacloud_assistant/models/assistant_conversation.py", names)
        self.assertFalse([n for n in names if "/tests/" in n])

    def test_no_row_is_locked_for_a_key_update(self):
        """No addon of core takes FOR UPDATE where FOR NO KEY UPDATE does."""
        violations = check(addon_files(_REPO))
        self.assertEqual(violations, [], "\n" + "\n".join(violations))


class TestTheLockCheckerCatchesWhatItMust(BaseCase):

    def _violations(self, body, temporary=None):
        """Return the violations of one probe module whose code is *body*."""
        return check_sources(
            {"addon/models/probe.py": textwrap.dedent(body)}, temporary,
        )

    def test_a_strong_lock_in_sql(self):
        """``FOR UPDATE``, split across strings as the code writes it."""
        found = self._violations("""
            class Probe:
                def _lock(self):
                    self.env.cr.execute(
                        "SELECT id FROM cloud_instance"
                        " WHERE id = %s FOR UPDATE", (1,),
                    )
        """)
        self.assertEqual(len(found), 1)
        self.assertIn("probe.py:5 Probe._lock", found[0])

    def test_a_strong_lock_inside_a_triple_quoted_query(self):
        """Its line is the one ``FOR UPDATE`` is on."""
        found = self._violations('''
            def claim(cr):
                cr.execute("""
                    SELECT id FROM cloud_instance
                    LIMIT 1 FOR UPDATE SKIP LOCKED
                """)
        ''')
        self.assertEqual(len(found), 1)
        self.assertIn("probe.py:5 claim", found[0])

    def test_the_soft_lock_passes(self):
        """``FOR NO KEY UPDATE`` and ``allow_referencing=True`` are the rule."""
        self.assertEqual(self._violations("""
            def lock(cr, records):
                cr.execute("SELECT id FROM t WHERE id = %s FOR NO KEY UPDATE", (1,))
                cr.execute("SELECT id FROM t FOR NO KEY UPDATE SKIP LOCKED")
                records.lock_for_update(allow_referencing=True)
                records.try_lock_for_update(allow_referencing=True, limit=1)
        """), [])

    def test_the_orm_locks_left_strong(self):
        """Without ``allow_referencing=True`` both ORM locks take FOR UPDATE."""
        found = self._violations("""
            def lock(records, soft):
                records.lock_for_update()
                records.try_lock_for_update(limit=1)
                records.lock_for_update(allow_referencing=soft)
        """)
        self.assertEqual(len(found), 3)

    def test_docstrings_are_not_code(self):
        """A docstring that explains the lock it avoids runs nothing."""
        self.assertEqual(self._violations('''
            """Not ``SELECT … FOR UPDATE``: see the method."""

            class Probe:
                """Locks ``FOR NO KEY UPDATE``, never ``FOR UPDATE``."""

                def _lock(self):
                    """``FOR UPDATE`` would mark the key as changed."""
        '''), [])

    def test_the_marker_allows_a_lock_that_deletes_the_row(self):
        """On the line before, or on one of the statement's own lines."""
        self.assertEqual(self._violations("""
            def purge(cr, records):
                # lock: key-update needed — the row is deleted below
                cr.execute("SELECT id FROM t WHERE id = %s FOR UPDATE", (1,))
                records.lock_for_update()  # lock: key-update needed — deleted
        """), [])

    def test_a_marker_without_a_reason_allows_nothing(self):
        """The reason is the point of the marker."""
        self.assertEqual(len(self._violations("""
            def purge(cr):
                # lock: key-update needed
                cr.execute("SELECT id FROM t WHERE id = %s FOR UPDATE", (1,))
        """)), 1)

    def test_a_temporary_exception_excuses_its_method_only(self):
        """Keyed on the file and the method, not on the file."""
        body = """
            class Probe:
                def _charge_lock(self):
                    self.env.cr.execute("SELECT 1 FOR UPDATE")

                def _other(self):
                    self.env.cr.execute("SELECT 2 FOR UPDATE")
        """
        found = self._violations(
            body, {("models/probe.py", "Probe._charge_lock"): "pending"},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("Probe._other", found[0])

    def test_a_temporary_exception_left_behind_is_reported(self):
        """Once its fix lands, the exception has to go."""
        found = self._violations(
            "def soft(cr):\n    cr.execute('SELECT 1 FOR NO KEY UPDATE')\n",
            {("models/probe.py", "soft"): "pending"},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("excuses nothing any more", found[0])
