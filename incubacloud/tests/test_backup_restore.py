"""Tier 1 — Pure-Python tests for BackupRestoreExecutor.get_commands().

Guards the invariant that a failed ``dup restore`` must abort before the
destructive dropdb/createdb steps, and that the SQL import fails on the
first error instead of reporting a partial restore as success.
"""
from types import SimpleNamespace

from odoo.tests.common import BaseCase


def _make_executor(time='2026-01-01T00:00:00', dbname='prod'):
    from odoo.addons.incubacloud.models.backup_restore_executor import (
        BackupRestoreExecutor,
    )
    inst = SimpleNamespace(
        name='inst',
        postgres_dbname=dbname,
        environment='production',
    )
    job = SimpleNamespace(instance_id=inst, payload={'time': time})
    ex = object.__new__(BackupRestoreExecutor)
    ex.job = job
    ex._inst_dir = lambda i: f"~/projects/{i.name}"
    return ex


def _find(cmds, label_substring):
    for tup in cmds:
        if label_substring in tup[0]:
            return tup
    raise AssertionError(f"No command labelled like {label_substring!r}")


class TestBackupRestoreCommands(BaseCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor().get_commands()

    def test_restore_step_has_stop_on_failure(self):
        # A failed dup restore must abort before dropdb/createdb, or the
        # production database is destroyed and a stale/absent dump imported.
        tup = _find(self.cmds, "Restore from backup")
        self.assertEqual(len(tup), 3, "restore step is missing its opts dict")
        self.assertTrue(tup[2].get("stop_on_failure"))

    def test_import_sql_uses_on_error_stop(self):
        # psql returns 0 on a partial import unless ON_ERROR_STOP is set,
        # which would report a broken restore as success.
        tup = _find(self.cmds, "Import SQL")
        self.assertIn("ON_ERROR_STOP=1", tup[1])

    def test_dropdb_createdb_import_stop_on_failure(self):
        for label in ("Drop database", "Create database", "Import SQL"):
            tup = _find(self.cmds, label)
            self.assertTrue(
                tup[2].get("stop_on_failure"),
                f"{label} must stop on failure",
            )

    def test_restore_precedes_dropdb(self):
        labels = [t[0] for t in self.cmds]
        self.assertLess(
            labels.index("Restore from backup"),
            labels.index("Drop database"),
        )
