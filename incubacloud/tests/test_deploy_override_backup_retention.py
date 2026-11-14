"""The backup retention set in the panel is the one the container runs.

The doodba template renders ``JOB_800_WHAT`` with ``3M`` in the backup
service's ``environment:`` whenever the duplicity cron owns the retention,
and Compose gives ``environment`` precedence over ``env_file``: the
retention written to ``.docker/backup.env`` never applied, and a 7D or 1Y
destination kept three months (14-nov-2026). The override wins over the
template, so the retention rides there.
"""
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)

from .test_deploy_override_logging import _OverrideCase


class TestOverrideBackupRetention(_OverrideCase):

    def setUp(self):
        super().setUp()
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
