"""Tests for BackupDownloadNeutralizedExecutor.get_commands().

Since Phase 3 the executor no longer composes bash: it calls
``scripts/backup_neutralized.sh`` with arguments. So what is worth
testing here is the *wiring* — which operation each step invokes, with
which arguments, in which order, and which steps abort the chain. The
behaviour of the shell itself (dup restore, click-odoo-restoredb, the
pg_dump branch, the root cleanup) is covered by
``tests/shell/backup_neutralized.bats``.
"""

import shlex
from types import SimpleNamespace

from odoo.tests.common import BaseCase

SCRIPT = "backup_neutralized.sh"
JOB_ID = 99
INSTANCE_DIR = "~/projects/demo-inst"
NEUTRAL_DB = f"__ic_neutral_{JOB_ID}"
HOST_TMP = f"/tmp/.incubacloud-bkneu-{JOB_ID}"


def _make_executor(environment='production', time='latest',
                   with_filestore=False, deployed=True):
    from odoo.addons.incubacloud.models.backup_download_neutralized_executor \
        import BackupDownloadNeutralizedExecutor

    inst = SimpleNamespace(
        id=7,
        name='demo-inst',
        environment=environment,
        postgres_dbname='prod',
        deployed=deployed,
    )
    job = SimpleNamespace(
        id=JOB_ID,
        instance_id=inst,
        payload={'time': time, 'with_filestore': with_filestore},
    )

    ex = object.__new__(BackupDownloadNeutralizedExecutor)
    ex.job = job
    ex._inst_dir = lambda i: INSTANCE_DIR
    ex._scripts_requested = False
    ex._script_overlay_cache = None
    return ex


def _find(cmds, label_substring):
    return next((c for c in cmds if label_substring in c[0]), None)


def _argv(step):
    """Return the script invocation of *step* as an argv list.

    ``['bash', '<remote path>', '<operation>', '<arg>', ...]``
    """
    return shlex.split(step[1])


class NeutralizedCase(BaseCase):

    def assertScriptCall(self, step, operation, args):
        """Assert *step* runs ``SCRIPT`` with *operation* and *args*."""
        argv = _argv(step)
        self.assertEqual(argv[0], "bash")
        self.assertTrue(argv[1].endswith(f"/{SCRIPT}"), argv[1])
        self.assertEqual(argv[2], operation)
        # Every operation takes <dir> <job_id> before its own arguments.
        self.assertEqual(argv[3:5], [INSTANCE_DIR, str(JOB_ID)])
        self.assertEqual(argv[5:], args)


class TestNeutralizedProd(NeutralizedCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(environment='production').get_commands()

    def test_step_count(self):
        self.assertEqual(len(self.cmds), 4)

    def test_steps_run_in_order(self):
        self.assertEqual(
            [c[0] for c in self.cmds],
            [
                "Restore source dump from S3",
                "Restore and neutralize in temp DB",
                "Create neutralized backup (SQL only)",
                "Drop temp DB and cleanup",
            ],
        )

    def test_prod_restores_from_the_backup_store(self):
        self.assertScriptCall(
            _find(self.cmds, "Restore source dump from S3"),
            "prepare-src-prod",
            ["prod", "latest", HOST_TMP],
        )

    def test_prod_does_not_dump_live(self):
        self.assertIsNone(_find(self.cmds, "Dump live database"))

    def test_neutralize_step_targets_the_throwaway_db(self):
        self.assertScriptCall(
            _find(self.cmds, "Restore and neutralize in temp DB"),
            "restore-neutralize",
            [NEUTRAL_DB],
        )

    def test_temp_db_name_isolated_per_job(self):
        argv = _argv(_find(self.cmds, "Restore and neutralize in temp DB"))
        self.assertIn(f"__ic_neutral_{JOB_ID}", argv)

    def test_cleanup_step_drops_the_temp_db(self):
        self.assertScriptCall(
            _find(self.cmds, "Drop temp DB and cleanup"),
            "cleanup",
            [NEUTRAL_DB, HOST_TMP],
        )

    def test_critical_steps_have_stop_on_failure(self):
        for label in (
            "Restore source dump from S3",
            "Restore and neutralize in temp DB",
        ):
            step = _find(self.cmds, label)
            self.assertEqual(len(step), 3, f"{label} missing opts")
            self.assertTrue(step[2].get("stop_on_failure"))

    def test_cleanup_has_no_stop_on_failure(self):
        # Cleanup is best-effort — it runs whether or not earlier steps
        # failed AND must not abort the chain itself.
        self.assertEqual(len(_find(self.cmds, "Drop temp DB and cleanup")), 2)

    def test_a_requested_time_reaches_the_script(self):
        cmds = _make_executor(
            environment='production', time='12h_ago',
        ).get_commands()
        self.assertScriptCall(
            _find(cmds, "Restore source dump from S3"),
            "prepare-src-prod",
            ["prod", "12h_ago", HOST_TMP],
        )


class TestNeutralizedProdWithoutFilestore(NeutralizedCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(
            environment='production', with_filestore=False,
        ).get_commands()

    def test_redump_is_sql_only(self):
        self.assertScriptCall(
            _find(self.cmds, "Create neutralized backup (SQL only)"),
            "redump-sql",
            [NEUTRAL_DB, HOST_TMP, f"{HOST_TMP}.zip"],
        )

    def test_no_filestore_branch(self):
        self.assertIsNone(
            _find(self.cmds, "Create neutralized backup (DB + filestore)"),
        )


class TestNeutralizedProdWithFilestore(NeutralizedCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(
            environment='production', with_filestore=True,
        ).get_commands()

    def test_redump_includes_the_filestore(self):
        self.assertScriptCall(
            _find(self.cmds, "Create neutralized backup (DB + filestore)"),
            "redump-full",
            [NEUTRAL_DB, f"{HOST_TMP}.zip"],
        )

    def test_no_sql_only_branch(self):
        self.assertIsNone(
            _find(self.cmds, "Create neutralized backup (SQL only)"),
        )


class TestNeutralizedStaging(NeutralizedCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(
            environment='staging', time='live',
        ).get_commands()

    def test_non_prod_dumps_the_live_db(self):
        self.assertScriptCall(
            _find(self.cmds, "Dump live database"),
            "prepare-src-live",
            ["prod", HOST_TMP],
        )

    def test_non_prod_skips_the_backup_store(self):
        self.assertIsNone(_find(self.cmds, "Restore source dump from S3"))

    def test_non_prod_still_neutralizes(self):
        self.assertScriptCall(
            _find(self.cmds, "Restore and neutralize in temp DB"),
            "restore-neutralize",
            [NEUTRAL_DB],
        )


class TestTempDbIsolation(BaseCase):

    def test_different_jobs_get_different_temp_dbs(self):
        from odoo.addons.incubacloud.models.\
backup_download_neutralized_executor import (
            BackupDownloadNeutralizedExecutor,
        )
        ex1 = _make_executor()
        ex1.job.id = 100
        ex2 = _make_executor()
        ex2.job.id = 200
        # Sanity check the convention is derived from job.id.
        name1 = BackupDownloadNeutralizedExecutor._tmp_neutral_db(ex1)
        name2 = BackupDownloadNeutralizedExecutor._tmp_neutral_db(ex2)
        self.assertEqual(name1, "__ic_neutral_100")
        self.assertEqual(name2, "__ic_neutral_200")
        self.assertNotEqual(name1, name2)
