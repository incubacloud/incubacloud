"""Tests for BackupDownloadNeutralizedExecutor.get_commands()."""

from types import SimpleNamespace

from odoo.tests.common import BaseCase


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
        id=99,
        instance_id=inst,
        payload={'time': time, 'with_filestore': with_filestore},
    )

    ex = object.__new__(BackupDownloadNeutralizedExecutor)
    ex.job = job
    ex._inst_dir = lambda i: f"~/projects/{i.name}"
    return ex


def _find(cmds, label_substring):
    return next((c for c in cmds if label_substring in c[0]), None)


class TestNeutralizedProd(BaseCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(environment='production').get_commands()

    def test_step_count(self):
        self.assertEqual(len(self.cmds), 4)

    def test_prod_uses_dup_restore(self):
        step = _find(self.cmds, "Restore source dump from S3")
        self.assertIsNotNone(step)
        self.assertIn("dup restore", step[1])
        self.assertIn("prod.sql", step[1])

    def test_prod_does_not_dump_live(self):
        step = _find(self.cmds, "Dump live database")
        self.assertIsNone(step)

    def test_restore_neutralize_uses_click_odoo_restoredb(self):
        step = _find(self.cmds, "Restore and neutralize in temp DB")
        self.assertIsNotNone(step)
        self.assertIn("click-odoo-restoredb", step[1])
        self.assertIn("--neutralize", step[1])
        self.assertIn("--force", step[1])

    def test_temp_db_name_isolated_per_job(self):
        step = _find(self.cmds, "Restore and neutralize in temp DB")
        self.assertIn("__ic_neutral_99", step[1])

    def test_cleanup_step_drops_temp_db(self):
        step = _find(self.cmds, "Drop temp DB and cleanup")
        self.assertIsNotNone(step)
        self.assertIn("dropdb --if-exists __ic_neutral_99", step[1])
        self.assertIn("rm -rf /var/lib/odoo/filestore/__ic_neutral_99",
                      step[1])

    def test_critical_steps_have_stop_on_failure(self):
        for label in (
            "Restore source dump from S3",
            "Restore and neutralize in temp DB",
        ):
            step = _find(self.cmds, label)
            self.assertEqual(len(step), 3, f"{label} missing opts")
            self.assertTrue(step[2].get("stop_on_failure"))

    def test_cleanup_has_no_stop_on_failure(self):
        step = _find(self.cmds, "Drop temp DB and cleanup")
        # Cleanup is best-effort — runs whether or not earlier steps fail
        # AND must not abort the chain itself.
        self.assertEqual(len(step), 2)


class TestNeutralizedProdWithoutFilestore(BaseCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(
            environment='production', with_filestore=False,
        ).get_commands()

    def test_redump_uses_pg_dump(self):
        step = _find(self.cmds, "Create neutralized backup (SQL only)")
        self.assertIsNotNone(step)
        self.assertIn("pg_dump", step[1])
        self.assertIn("--dbname=__ic_neutral_99", step[1])

    def test_redump_zips_to_archive(self):
        step = _find(self.cmds, "Create neutralized backup (SQL only)")
        self.assertIn("zip -r", step[1])
        self.assertIn("dump.sql", step[1])

    def test_redump_does_not_include_filestore_archive(self):
        step = _find(self.cmds, "Create neutralized backup (SQL only)")
        self.assertNotIn("click-odoo-backupdb", step[1])


class TestNeutralizedProdWithFilestore(BaseCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(
            environment='production', with_filestore=True,
        ).get_commands()

    def test_redump_uses_click_odoo_backupdb(self):
        step = _find(self.cmds, "Create neutralized backup (DB + filestore)")
        self.assertIsNotNone(step)
        self.assertIn("click-odoo-backupdb", step[1])
        self.assertIn("__ic_neutral_99", step[1])

    def test_no_pg_dump_branch(self):
        step = _find(self.cmds, "Create neutralized backup (SQL only)")
        self.assertIsNone(step)


class TestNeutralizedStaging(BaseCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cmds = _make_executor(
            environment='staging', time='live',
        ).get_commands()

    def test_non_prod_dumps_live_db(self):
        step = _find(self.cmds, "Dump live database")
        self.assertIsNotNone(step)
        self.assertIn("click-odoo-backupdb prod", step[1])

    def test_non_prod_skips_dup_restore(self):
        step = _find(self.cmds, "Restore source dump from S3")
        self.assertIsNone(step)

    def test_non_prod_still_neutralizes(self):
        step = _find(self.cmds, "Restore and neutralize in temp DB")
        self.assertIsNotNone(step)
        self.assertIn("--neutralize", step[1])

    def test_non_prod_source_file_is_zip(self):
        # Live dump via click-odoo-backupdb produces a .zip (with
        # filestore) so restoredb must consume the zip, not a .sql.
        step = _find(self.cmds, "Restore and neutralize in temp DB")
        self.assertIn("/tmp/bkneu-src-99.zip", step[1])


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
