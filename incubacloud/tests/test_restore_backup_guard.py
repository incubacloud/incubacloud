"""Tests for the pre-restore safety-backup guard (Imprescindible #3).

``restore_backup`` (production, from a duplicity snapshot) and
``restore_db`` (zip upload/rsync/from_job) must always take a fresh
``backup_create`` snapshot first on production instances — no opt-out.
Non-production instances only chain the safety backup when the caller
opts in via ``payload['backup_before_restore']``.

Mirrors the chain-shape testing technique of test_cloud_instance_move.py:
``cloud.job.enqueue``/``enqueue_chain`` are patched so the SSH executors
never run; only the orchestration in cloud.instance is under test.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestRestoreBackupGuard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "restore-proj"})
        self.host = self.env["cloud.host"].create(
            {
                "name": "restore-host",
                "ip_address": "10.0.0.9",
                "port": 22,
                "user": "root",
                "login_type": "ssh_key",
                "wildcard_domain": "restore.example.com",
                "status": "compatible",
                "traefik_deployed": True,
            }
        )
        self.bb = self.env["cloud.backup.backend"].create(
            {
                "name": "restore-bb",
                "backend_type": "s3",
                "s3_bucket": "restore-bucket",
            }
        )
        self.prod = self.env["cloud.instance"].create(
            {
                "name": "prod-inst",
                "project_id": self.project.id,
                "environment": "production",
                "host_id": self.host.id,
                "state": "deployed",
                "backup_backend_id": self.bb.id,
            }
        )
        self.staging = self.env["cloud.instance"].create(
            {
                "name": "staging-inst",
                "project_id": self.project.id,
                "environment": "staging",
                "host_id": self.host.id,
                "state": "deployed",
                "backup_backend_id": self.bb.id,
            }
        )
        self.no_backend_staging = self.env["cloud.instance"].create(
            {
                "name": "no-backend-staging",
                "project_id": self.project.id,
                "environment": "staging",
                "host_id": self.host.id,
                "state": "deployed",
            }
        )

    def _patch_chain(self, return_value=(1, 2, 3)):
        return patch.object(
            type(self.env["cloud.job"]),
            "enqueue_chain",
            return_value=list(return_value),
        )

    def _patch_enqueue(self, return_value=9):
        return patch.object(
            type(self.env["cloud.job"]),
            "enqueue",
            return_value=return_value,
        )

    # ── restore_backup (production only) ─────────────────────────────

    def test_restore_backup_prod_chains_backup_create(self):
        with self._patch_chain() as m:
            job_id = self.prod.restore_backup({"time": "2026-01-01T00:00:00"})
        self.assertEqual(job_id, 1)
        steps = m.call_args[0][0]
        self.assertEqual(
            [s["job_type_code"] for s in steps],
            ["backup_create", "backup_restore"],
        )
        self.assertEqual(steps[1]["payload"], {"time": "2026-01-01T00:00:00"})
        self.assertTrue(all(s["host_id"] == self.host.id for s in steps))
        self.assertTrue(all(s["instance_id"] == self.prod.id for s in steps))

    def test_restore_backup_prod_no_backend_raises(self):
        self.prod.backup_backend_id = False
        with self._patch_chain():
            with self.assertRaises(UserError):
                self.prod.restore_backup({"time": "2026-01-01T00:00:00"})

    # ── restore_db: production forces the chain regardless of flag ───

    def test_restore_db_production_forces_backup_even_if_flag_false(self):
        payload = {"mode": "rsync", "backup_before_restore": False}
        with self._patch_chain() as m:
            job_id = self.prod.restore_db(payload)
        self.assertEqual(job_id, 1)
        steps = m.call_args[0][0]
        self.assertEqual(
            [s["job_type_code"] for s in steps],
            ["backup_create", "backup_download", "restore_instance"],
        )
        self.assertEqual(steps[2]["payload"], payload)
        self.assertEqual(
            steps[1]["payload"],
            {"time": "latest", "download_type": "all"},
        )

    def test_restore_db_prod_no_backend_raises(self):
        self.prod.backup_backend_id = False
        with self._patch_chain():
            with self.assertRaises(UserError):
                self.prod.restore_db({"mode": "rsync"})

    # ── restore_db: non-production defaults to no safety backup ──────

    def test_restore_db_staging_default_no_backup(self):
        with self._patch_enqueue() as m, self._patch_chain() as chain_mock:
            job_id = self.staging.restore_db({"mode": "rsync"})
        self.assertEqual(job_id, 9)
        chain_mock.assert_not_called()
        m.assert_called_once_with(
            self.host.id,
            self.staging.id,
            "restore_instance",
            payload={"mode": "rsync"},
        )

    def test_restore_db_staging_flag_true_chains_backup(self):
        payload = {"mode": "rsync", "backup_before_restore": True}
        with self._patch_chain() as m:
            job_id = self.staging.restore_db(payload)
        self.assertEqual(job_id, 1)
        steps = m.call_args[0][0]
        self.assertEqual(
            [s["job_type_code"] for s in steps],
            ["backup_create", "backup_download", "restore_instance"],
        )
        self.assertEqual(steps[2]["payload"], payload)

    def test_restore_db_staging_flag_true_no_backend_raises(self):
        payload = {"mode": "rsync", "backup_before_restore": True}
        with self._patch_chain():
            with self.assertRaises(UserError):
                self.no_backend_staging.restore_db(payload)

    def test_restore_db_from_job_mode_keeps_original_source_job_id(self):
        # The chained backup_create/backup_download steps are a parallel
        # safety net — they must never overwrite a from_job restore's own
        # reference to a caller-specified source job.
        payload = {
            "mode": "from_job",
            "source_job_id": "42",
            "backup_before_restore": True,
        }
        with self._patch_chain() as m:
            self.staging.restore_db(payload)
        steps = m.call_args[0][0]
        self.assertEqual(steps[2]["payload"]["source_job_id"], "42")
