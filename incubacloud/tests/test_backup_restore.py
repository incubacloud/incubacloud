"""Tier 1 — the ordering + abort contract of BackupRestoreExecutor.

Since Phase 3 the container-side steps run ``scripts/backup_restore.sh``
instead of composing bash, so this pins the *wiring*: the restore step
aborts before the destructive dropdb/createdb steps, each destructive
step stops on failure, and each step invokes the right script operation.
The shell behaviour (the ON_ERROR_STOP import, the ``$SRC``/``$DST``
isolation) is covered by ``tests/shell/backup_restore.bats``.
"""
import shlex
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
    job = SimpleNamespace(id=99, instance_id=inst, payload={'time': time})
    ex = object.__new__(BackupRestoreExecutor)
    ex.job = job
    ex._inst_dir = lambda i: f"~/projects/{i.name}"
    ex._scripts_requested = False
    ex._scripts_uploaded = False
    ex._script_overlay_cache = None
    return ex


def _find(cmds, label_substring):
    for tup in cmds:
        if label_substring in tup[0]:
            return tup
    raise AssertionError(f"No command labelled like {label_substring!r}")


def _op(tup):
    """Return the script operation a command invokes."""
    return shlex.split(tup[1])[2]


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

    def test_each_step_invokes_its_script_operation(self):
        self.assertEqual(_op(_find(self.cmds, "Restore from backup")), "restore")
        self.assertEqual(_op(_find(self.cmds, "Drop database")), "dropdb")
        self.assertEqual(_op(_find(self.cmds, "Create database")), "createdb")
        self.assertEqual(_op(_find(self.cmds, "Import SQL")), "import-sql")

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
