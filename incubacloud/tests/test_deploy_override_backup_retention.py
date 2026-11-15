"""The backup retention set in the panel is the one the container runs.

The doodba template renders ``JOB_800_WHAT`` with ``3M`` in the backup
service's ``environment:`` whenever the duplicity cron owns the retention,
and Compose gives ``environment`` precedence over ``env_file``: the
retention written to ``.docker/backup.env`` never applied, and a 7D or 1Y
destination kept three months (14-nov-2026). The override wins over the
template, so the retention rides there.

The retention reaches a shell in the container, so only ``<count><unit>``
is accepted, and the configuration snapshot carries it once it is not the
template's: an instance deployed before keeps a change to apply.
"""
import importlib.util
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase
from odoo.tools.misc import file_path

from odoo.addons.incubacloud.models._backup_retention import (
    valid_backup_retention,
)
from odoo.addons.incubacloud.models.cloud_instance import CloudInstance
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)

from ._template import pin_template
from .test_deploy_override_logging import _OverrideCase

#: The template that rendered ``JOB_800_WHAT`` over ``backup.env``.
_V10 = "v10.0.8"


class TestOverrideBackupRetention(_OverrideCase):

    def setUp(self):
        super().setUp()
        # The failure was the v10 template's: test with it.
        pin_template(self.env, _V10)
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )

    def _with_backend(self, retention, owner="cron"):
        """Give the instance a backup destination with *retention*/*owner*."""
        backend = self.env["cloud.backup.backend"].create({
            "name": f"ret-{retention}-{owner}",
            "backend_type": "s3",
            "s3_bucket": "ret-bucket",
            "backup_retention": retention,
            "retention_owner": owner,
        })
        self.instance.backup_backend_id = backend
        self.assertIn("backup", self.instance.expected_services())

    def _backup_env(self, executor_cls=DeployInstanceExecutor,
                    job_type="deploy_instance"):
        """Return the backup service's override ``environment``."""
        _raw, data = self._override(self._executor(executor_cls, job_type))
        return data["services"]["backup"].get("environment", {})

    def test_cron_retention_rides_the_override(self):
        self._with_backend("7D")
        self.assertEqual(
            self._backup_env()["JOB_800_WHAT"],
            "dup --force remove-older-than 7D $$DST",
        )

    def test_rebuild_carries_it_too(self):
        """Instances already deployed take it on their next rebuild."""
        self._with_backend("1Y")
        env = self._backup_env(RebuildInstanceExecutor, "rebuild_instance")
        self.assertEqual(
            env["JOB_800_WHAT"], "dup --force remove-older-than 1Y $$DST",
        )

    def test_default_3m_is_stated_as_well(self):
        """The panel's value governs even when it equals the template's."""
        self._with_backend("3M")
        self.assertEqual(
            self._backup_env()["JOB_800_WHAT"],
            "dup --force remove-older-than 3M $$DST",
        )

    def test_no_deletion_job_when_the_cron_does_not_delete(self):
        for owner in ("none", "lifecycle"):
            with self.subTest(owner=owner):
                self._with_backend("14D", owner=owner)
                self.assertNotIn("JOB_800_WHAT", self._backup_env())

    def test_backup_env_file_has_no_line_when_nobody_deletes(self):
        for owner in ("none", "lifecycle"):
            with self.subTest(owner=owner):
                self._with_backend("14D", owner=owner)
                executor = self._executor(DeployInstanceExecutor, "deploy_instance")
                self.assertNotIn("JOB_800_WHAT", executor._backup_env_content())

    def test_no_backup_service_no_entry(self):
        """Without a destination there is no backup service to override."""
        _raw, data = self._override(
            self._executor(DeployInstanceExecutor, "deploy_instance"),
        )
        self.assertNotIn("backup", data["services"])

    def _store_retention(self, retention):
        """Store *retention* past the constraint, as an old database holds it."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_backup_backend SET backup_retention = %s WHERE id = %s",
            (retention, self.instance.backup_backend_id.id),
        )
        self.instance.backup_backend_id.invalidate_recordset(["backup_retention"])

    def test_a_retention_the_shell_must_not_run_writes_no_entry(self):
        """``1m`` is one minute to duplicity; ``;`` starts a command."""
        self._with_backend("7D")
        for stored in ("1m", "7D; touch /tmp/x", "0D", "7 D", "D"):
            with self.subTest(retention=stored):
                self._store_retention(stored)
                executor = self._executor(DeployInstanceExecutor, "deploy_instance")
                _raw, data = self._override(executor)
                env = data["services"]["backup"].get("environment", {})
                self.assertNotIn("JOB_800_WHAT", env)
                self.assertNotIn("JOB_800_WHAT", executor._backup_env_content())
                warning = [m for m, _k in executor._log_buffer if "not applied" in m]
                self.assertEqual(len(warning), 1, executor._log_buffer)
                self.assertIn(repr(stored), warning[0])

    def test_the_log_says_which_retention_applies(self):
        self._with_backend("7D")
        for executor_cls, job_type in (
            (DeployInstanceExecutor, "deploy_instance"),
            (RebuildInstanceExecutor, "rebuild_instance"),
        ):
            with self.subTest(job_type=job_type):
                executor = self._executor(executor_cls, job_type)
                self._override(executor)
                self.assertIn(
                    "Backup retention: 7D, applied by the weekly cleanup.",
                    [m for m, _k in executor._log_buffer],
                )

    def test_the_log_is_silent_when_the_cron_does_not_delete(self):
        self._with_backend("7D", owner="lifecycle")
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        self._override(executor)
        self.assertFalse(
            [m for m, _k in executor._log_buffer if "retention" in m.lower()],
        )

    def test_the_model_is_where_the_retention_is_read(self):
        """The executor delegates, as it does ``_backup_enabled``."""
        self._with_backend("14D")
        self.assertEqual(self.instance._backup_retention(), "14D")
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        self.assertEqual(executor._backup_retention(), "14D")


class TestBackupRetentionConstraint(_OverrideCase):

    def test_valid_retentions(self):
        for value in ("1D", "7D", "2W", "1M", "3M", "12M", "1Y", " 7D "):
            with self.subTest(value=value):
                self.assertTrue(valid_backup_retention(value))
        for value in ("", None, "0D", "07", "1m", "7d", "7D; x", "7D\nx",
                      "7 D", "D", "1H", "\u0661D", "3M$(id)"):
            with self.subTest(value=value):
                self.assertFalse(valid_backup_retention(value))

    def test_a_destination_refuses_an_invalid_retention(self):
        backend = self.env["cloud.backup.backend"].create({
            "name": "ret-check", "backend_type": "s3", "s3_bucket": "rc",
        })
        self.assertEqual(backend.backup_retention, "3M")
        for value in ("1m", "7D; x", "0D", ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    backend.backup_retention = value
                    backend.flush_recordset()
        backend.backup_retention = "2W"
        self.assertEqual(backend.backup_retention, "2W")


class TestBackupRetentionSnapshot(_OverrideCase):
    """The snapshot carries the retention only once it is not the template's."""

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "snap-ret", "backend_type": "s3", "s3_bucket": "snap",
        })
        self.instance.backup_backend_id = self.backend

    def _snapshot(self):
        """Render the instance's snapshot afresh."""
        self.instance.invalidate_recordset()
        return self.instance._render_config_snapshot()

    def _hash_without_the_key(self):
        """The instance's hash as it was before the retention had a key."""
        with patch.object(
            CloudInstance, "_cron_backup_retention", autospec=True,
            return_value=None,
        ):
            self.instance.invalidate_recordset()
            return self.instance._config_snapshot_hash()

    def _assert_hash_unchanged(self):
        """The snapshot has no retention key and the hash did not move."""
        self.assertEqual(set(self._snapshot()), {"answers", "repos", "extras"})
        self.assertEqual(
            self.instance._config_snapshot_hash(), self._hash_without_the_key(),
        )

    def test_the_rest_of_the_fleet_hashes_as_before(self):
        """3M, a cleanup by the bucket or nobody, no backups: no key."""
        self._assert_hash_unchanged()
        for owner in ("lifecycle", "none"):
            with self.subTest(owner=owner):
                self.backend.write({"backup_retention": "7D", "retention_owner": owner})
                self._assert_hash_unchanged()
        self.backend.retention_owner = "cron"
        self.instance.backup_backend_id = False
        self._assert_hash_unchanged()

    def test_a_retention_other_than_3m_moves_the_hash(self):
        self.backend.backup_retention = "7D"
        self.assertNotEqual(
            self.instance._config_snapshot_hash(), self._hash_without_the_key(),
        )

    def test_a_retention_the_cron_applies_is_a_change_to_apply(self):
        self.instance.write(self.instance._applied_config_vals())
        self.backend.backup_retention = "7D"
        self.assertEqual(self._snapshot()["backup_retention"], "7D")
        self.instance.invalidate_recordset(["config_dirty"])
        self.assertTrue(self.instance.config_dirty)
        # Named as it reads, among the pending changes.
        self.assertEqual(self.instance._config_drift_diff(), ["backup_retention"])

    def test_an_invalid_retention_is_not_one(self):
        """The deploy writes nothing for it, so nothing is pending."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_backup_backend SET backup_retention = '1m' WHERE id = %s",
            (self.backend.id,),
        )
        self.backend.invalidate_recordset(["backup_retention"])
        self._assert_hash_unchanged()


class TestBackupRetentionMigration(TransactionCase):
    """1.0.232 writes the stored retentions in the form they must have."""

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "post_migrate_1_0_232",
            file_path("incubacloud/migrations/1.0.232/post-migrate.py"),
        )
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)

    def _stored(self, retention):
        """A destination holding *retention* as an older database may."""
        backend = self.env["cloud.backup.backend"].create({
            "name": f"mig-{retention!r}", "backend_type": "s3", "s3_bucket": "mig",
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_backup_backend SET backup_retention = %s WHERE id = %s",
            (retention, backend.id),
        )
        return backend

    def test_normalises_what_it_can_and_names_the_rest(self):
        cases = {
            " 7d ": "7D", "1m": "1M", "": "3M", None: "3M", "14D": "14D",
            "7D; x": "7D; x", "0D": "0D",
        }
        backends = {value: self._stored(value) for value in cases}
        with self.assertLogs(self.migration.__name__, "WARNING") as logs:
            self.migration.migrate(self.env.cr, "19.0.1.0.228")
        self.env.invalidate_all()
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(backends[value].backup_retention, expected)
        warned = "\n".join(logs.output)
        self.assertIn("'7D; x'", warned)
        self.assertIn("'0D'", warned)
        self.assertNotIn("'1m'", warned)

    def test_a_second_run_changes_nothing(self):
        backend = self._stored("1m")
        self.migration.migrate(self.env.cr, "19.0.1.0.228")
        with patch.object(self.migration._logger, "info", autospec=True) as info:
            self.migration.migrate(self.env.cr, "19.0.1.0.228")
        self.assertNotIn(backend.id, [c.args[1] for c in info.call_args_list])
        self.env.invalidate_all()
        self.assertEqual(backend.backup_retention, "1M")
