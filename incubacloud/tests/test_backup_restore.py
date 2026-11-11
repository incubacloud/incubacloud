"""Tier 1 — the ordering + abort contract of BackupRestoreExecutor.

Since Phase 3 the container-side steps run ``scripts/backup_restore.sh``
instead of composing bash, so this pins the *wiring*: the restore step
aborts before the destructive dropdb/createdb steps, each destructive
step stops on failure, and each step invokes the right script operation.
The shell behaviour (the ON_ERROR_STOP import, the ``$SRC``/``$DST``
isolation) is covered by ``tests/shell/backup_restore.bats``.
"""
import asyncio
import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase


def _make_executor(time='2026-01-01T00:00:00', dbname='prod',
                   auto_update=True):
    from odoo.addons.incubacloud.models.backup_restore_executor import (
        BackupRestoreExecutor,
    )
    inst = SimpleNamespace(
        name='inst',
        postgres_dbname=dbname,
        postgres_username='odoo',
        environment='production',
        auto_update=auto_update,
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

    def test_latest_reaches_duplicity_as_now(self):
        # ``dup restore --time latest`` is refused: every revive of an
        # archived copy, which asks for 'latest', failed on it.
        cmds = _make_executor(time='latest').get_commands()
        step = _find(cmds, "Restore from backup")[1]
        self.assertNotIn('latest', step)
        self.assertIn('now', step)

    def test_the_health_probe_waits_for_a_restore(self):
        # It stops Odoo; graded then, an instance being revived goes to draft.
        from odoo.addons.incubacloud.models.instance_health_executor import (
            _BLOCKING_JOB_TYPES,
        )
        self.assertIn('backup_restore', _BLOCKING_JOB_TYPES)

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



class TestBackupRestoreUpdatesModules(BaseCase):
    """A restored backup gets its modules updated to the instance's code.

    The backup was taken under the code of its day, and a revive rebuilds
    the instance from its branch: Odoo started on it as it came served
    today's code over that day's schema until the next rebuild.
    """

    _LABEL = "Update changed modules"

    def test_the_update_runs_between_the_import_and_the_start(self):
        labels = [t[0] for t in _make_executor().get_commands()]
        self.assertLess(labels.index("Import SQL"), labels.index(self._LABEL))
        self.assertLess(
            labels.index(self._LABEL), labels.index("Start instance"),
        )

    def test_it_is_the_rebuild_s_update_and_stops_the_job(self):
        step = _find(_make_executor(dbname='main').get_commands(), self._LABEL)
        argv = shlex.split(step[1])
        self.assertTrue(argv[1].endswith("/cron_guard.sh"))
        self.assertEqual(argv[2:], ["update", "~/projects/inst", "main", "odoo"])
        self.assertTrue(step[2].get("stop_on_failure"))

    def test_an_instance_without_auto_update_is_left_alone(self):
        labels = [
            t[0] for t in _make_executor(auto_update=False).get_commands()
        ]
        self.assertNotIn(self._LABEL, labels)
        self.assertEqual(labels[-1], "Start instance")

    def test_a_failed_update_leaves_the_instance_in_error_saying_why(self):
        from odoo.addons.incubacloud.models.backup_restore_executor import (
            BackupRestoreExecutor,
        )
        from odoo.addons.incubacloud.models.cloud_instance import CloudInstance

        ex = _make_executor()
        inst = MagicMock(spec=CloudInstance)
        ex.job.instance_id = inst
        lines = []
        with patch.object(
            BackupRestoreExecutor, "_sys", autospec=True,
            side_effect=lambda _self, line: lines.append(line),
        ):
            asyncio.run(ex.on_failure(
                {self._LABEL: {"exit_status": 1, "stdout": ""}},
                [f"'{self._LABEL}' exited with status 1"],
            ))
        inst.write.assert_called_once_with({'status': 'error'})
        self.assertTrue(any("Odoo was left stopped" in line for line in lines))
        self.assertFalse(any("inconsistent state" in line for line in lines))


class TestBackupRestorePointOfNoReturn(BaseCase):
    """From the drop of the database on, a stop of a backup's restore is refused.

    Stopped there the database is left missing, half loaded or half
    updated, and the stop only kills the ``docker compose exec`` client:
    ``psql`` goes on inside the container.
    """

    def test_the_drop_is_the_point(self):
        ex = _make_executor()
        self.assertEqual(ex._no_return_from, "Drop database")
        labels = [t[0] for t in ex.get_commands()]
        self.assertIn(ex._no_return_from, labels)


class TestBackupRestoreCutOff(BaseCase):
    """A backup's restore cut off halfway ends in error, saying where.

    A connection that dropped, or a step out of its time, ended the run
    with no outcome: Odoo stopped, the instance said nothing of it.
    """

    def _abort(self, ran, running, exc=None):
        """Run ``on_abort`` after *ran*, with *running* cut off."""
        from odoo.addons.incubacloud.models.backup_restore_executor import (
            BackupRestoreExecutor,
        )
        from odoo.addons.incubacloud.models.cloud_instance import CloudInstance

        ex = _make_executor()
        inst = MagicMock(spec=CloudInstance)
        ex.job.instance_id = inst
        ex._step_results = ran
        ex._running_step = running
        lines = []
        with patch.object(
            BackupRestoreExecutor, "_sys", autospec=True,
            side_effect=lambda _self, line: lines.append(line),
        ):
            asyncio.run(ex.on_abort(exc or TimeoutError("3600 s")))
        return inst, lines

    def test_an_update_cut_off_leaves_the_instance_in_error(self):
        inst, lines = self._abort(
            {"Import SQL": {"exit_status": 0, "stdout": ""}},
            "Update changed modules",
        )
        inst.write.assert_called_once_with({'status': 'error'})
        self.assertTrue(any("did not end" in line for line in lines))
        self.assertTrue(any("Odoo was left stopped" in line for line in lines))

    def test_an_import_cut_off_says_the_state_may_be_inconsistent(self):
        inst, lines = self._abort({}, "Import SQL")
        inst.write.assert_called_once_with({'status': 'error'})
        self.assertTrue(any("'Import SQL' did not end" in line for line in lines))
        self.assertTrue(any("inconsistent state" in line for line in lines))

    def test_a_run_cut_before_its_steps_leaves_the_instance_alone(self):
        inst, lines = self._abort(None, None, exc=RuntimeError("no host"))
        inst.write.assert_not_called()
        self.assertEqual(lines, ["✗ RuntimeError: no host"])


class TestBackupRestoreSaysWhichCode(BaseCase):
    """The log says which code a restored backup's modules are updated to.

    Restoring production to undo a bad deployment, with the code still the
    deployment's, migrates the database again: the log names the branches
    and pinned commits before the restore starts.
    """

    def _said(self, auto_update=True):
        """Run ``before_execute``; return what it wrote to the log."""
        from odoo.addons.incubacloud.models.backup_restore_executor import (
            BackupRestoreExecutor,
        )
        ex = _make_executor(auto_update=auto_update)
        inst = ex.job.instance_id
        inst.odoo_version = '17.0'
        inst.odoo_commit_sha = False
        inst.repo_ids = [
            SimpleNamespace(
                url='https://github.com/acme/addons.git', branch='17.0',
                commit_sha=False,
            ),
            SimpleNamespace(
                url='https://gitlab.example.com/acme/sub/extra', branch='main',
                commit_sha='0123456789abcdef',
            ),
        ]
        lines = []
        with patch.object(
            BackupRestoreExecutor, "_sys", autospec=True,
            side_effect=lambda _self, line: lines.append(line),
        ):
            asyncio.run(ex.before_execute(None))
        return lines

    def test_it_names_the_branches_and_pins(self):
        said = self._said()
        self.assertEqual(len(said), 1)
        self.assertIn("Odoo 17.0", said[0])
        self.assertIn("acme/addons on 17.0", said[0])
        self.assertIn("acme/sub/extra on main pinned to 0123456789ab", said[0])
        self.assertIn("revert its code first", said[0])

    def test_without_auto_update_it_says_nothing(self):
        self.assertEqual(self._said(auto_update=False), [])
