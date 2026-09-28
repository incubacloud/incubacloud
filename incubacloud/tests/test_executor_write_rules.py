"""Core's executors follow the write rule, and the checker can tell.

The rule and why it exists are in ``executor_write_rules``. The second
class matters as much as the first: a checker that never fires passes
every repository, so it is fed code that breaks the rule on purpose.
"""
import textwrap
from pathlib import Path

from odoo.tests.common import BaseCase

from .executor_write_rules import (
    check,
    check_sources,
    executors_in,
    model_files,
)

_ADDON = Path(__file__).resolve().parent.parent


class TestCoreExecutorsFollowTheWriteRule(BaseCase):

    def test_there_are_executors_to_check(self):
        """The check found core's executors, so its silence means something."""
        self.assertIn(
            "DeployInstanceExecutor", executors_in(model_files(_ADDON)),
        )

    def test_no_executor_writes_in_the_jobs_transaction(self):
        """No core executor breaks the rule."""
        violations = check(model_files(_ADDON))
        self.assertEqual(violations, [], "\n" + "\n".join(violations))


class TestTheCheckerCatchesWhatItMust(BaseCase):

    _BASE = "class AbstractExecutor:\n    pass\n"

    def _violations(self, body, base="AbstractExecutor", context=None):
        """Return the violations of one probe executor whose methods are *body*."""
        source = f"class Probe({base}):\n" + textwrap.indent(
            textwrap.dedent(body), "    ",
        )
        return check_sources(
            {"abstract_executor.py": self._BASE, "probe.py": source},
            context=context,
        )

    def test_a_write_from_the_jobs_transaction(self):
        """A plain write in a job phase is flagged."""
        found = self._violations("""
            def before_execute(self, transport):
                self.env['cloud.instance'].browse(1).write({'x': 1})
        """)
        self.assertEqual(len(found), 1)
        self.assertIn("Probe.before_execute", found[0])

    def test_a_write_inside_a_durable_environment(self):
        """The same write inside _durable_env() is not."""
        self.assertEqual(self._violations("""
            def before_execute(self, transport):
                with self._durable_env() as env:
                    env['cloud.instance'].browse(1).write({'x': 1})
        """), [])

    def test_a_write_from_a_hook(self):
        """Hooks write on a durable cursor already."""
        self.assertEqual(self._violations("""
            async def on_success(self, results):
                self.env['cloud.instance'].browse(1).write({'x': 1})
        """), [])

    def test_an_enqueue_from_the_jobs_transaction(self):
        """Measured: the queue link of the new job is lost at the end."""
        self.assertEqual(len(self._violations("""
            def get_commands(self):
                self.env['cloud.job'].enqueue(1, 2, 'x')
                return []
        """)), 1)

    def test_a_cursor_of_its_own(self):
        """Opening a cursor outside the primitives is flagged."""
        found = self._violations("""
            def before_execute(self, transport):
                with self.job.env.registry.cursor() as cr:
                    pass
        """)
        self.assertTrue(any("opens its own cursor" in v for v in found))

    def test_a_hook_helper_called_outside_a_hook(self):
        """An allowed hook helper called from a job phase is flagged."""
        found = self._violations("""
            def _stamp_core_commit(self):
                self.env['cloud.instance'].browse(1).write({'x': 1})

            def before_execute(self, transport):
                self._stamp_core_commit()
        """)
        self.assertEqual(len(found), 1)
        self.assertIn("calls _stamp_core_commit()", found[0])

    def test_a_hook_helper_called_from_its_hook(self):
        """The same helper called from its hook is not."""
        self.assertEqual(self._violations("""
            def _stamp_core_commit(self):
                self.env['cloud.instance'].browse(1).write({'x': 1})

            async def on_success(self, results):
                self._stamp_core_commit()
        """), [])

    def test_the_filesystem_is_not_the_orm(self):
        """Path(...).unlink() is not a database write."""
        self.assertEqual(self._violations("""
            def before_execute(self, transport):
                Path('/tmp/x').unlink()
        """), [])

    def test_a_subclass_of_an_executor_from_another_repository(self):
        """How saas is checked: core's classes resolve its bases."""
        context = {
            "deploy.py": "class DeployInstanceExecutor(AbstractExecutor):\n"
                         "    pass\n",
        }
        self.assertEqual(len(self._violations("""
            def before_execute(self, transport):
                self._inst().write({'x': 1})
        """, base="DeployInstanceExecutor", context=context)), 1)

    def test_a_class_that_is_not_an_executor(self):
        """Models outside the executor hierarchy are not checked."""
        self.assertEqual(self._violations("""
            def action(self):
                self.write({'x': 1})
        """, base="models.Model"), [])
