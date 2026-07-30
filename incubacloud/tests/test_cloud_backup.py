"""
Tier 2 — ORM integration tests for cloud.backup.backend.
Tests run inside a TransactionCase (full DB rollback after each test).
"""

from odoo.tests.common import TransactionCase


class TestCloudBackupBackendPassphrase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Backend = self.env["cloud.backup.backend"]

    def _create(self, **kw):
        return self.Backend.create({"name": "Test Backend", "backend_type": "s3"} | kw)

    def test_create_auto_generates_passphrase(self):
        b = self._create(s3_bucket="my-bucket")
        self.assertTrue(b.passphrase)
        self.assertGreater(len(b.passphrase), 8)

    def test_create_explicit_passphrase_preserved(self):
        b = self._create(s3_bucket="my-bucket", passphrase="explicit-pass")
        self.assertEqual(b.passphrase, "explicit-pass")

    def test_write_empty_passphrase_keeps_existing(self):
        b = self._create(s3_bucket="my-bucket")
        original = b.passphrase
        b.write({"passphrase": ""})
        self.assertEqual(b.passphrase, original)

    def test_write_new_passphrase_updates(self):
        b = self._create(s3_bucket="my-bucket")
        b.write({"passphrase": "new-passphrase"})
        self.assertEqual(b.passphrase, "new-passphrase")

    def test_write_empty_secret_key_keeps_existing(self):
        b = self._create(
            s3_bucket="my-bucket",
            s3_secret_access_key="original-key",
        )
        b.write({"s3_secret_access_key": ""})
        self.assertEqual(b.s3_secret_access_key, "original-key")

    def test_write_new_secret_key_updates(self):
        b = self._create(s3_bucket="my-bucket", s3_secret_access_key="old")
        b.write({"s3_secret_access_key": "new-key"})
        self.assertEqual(b.s3_secret_access_key, "new-key")


class TestCloudBackupBackendDst(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Backend = self.env["cloud.backup.backend"]

    def _create(self, **kw):
        return self.Backend.create({"name": "DST Test", "backend_type": "s3"} | kw)

    def test_bucket_and_path(self):
        b = self._create(s3_bucket="my-bucket", s3_path="backups")
        self.assertEqual(b.backup_dst, "boto3+s3://my-bucket/backups")

    def test_bucket_no_path(self):
        b = self._create(s3_bucket="my-bucket", s3_path="")
        self.assertEqual(b.backup_dst, "boto3+s3://my-bucket")

    def test_path_with_leading_trailing_slashes_stripped(self):
        b = self._create(s3_bucket="my-bucket", s3_path="/data/")
        self.assertEqual(b.backup_dst, "boto3+s3://my-bucket/data")

    def test_no_bucket_gives_empty_dst(self):
        b = self._create()  # no s3_bucket
        self.assertEqual(b.backup_dst, "")

    def test_dst_updates_when_bucket_changes(self):
        b = self._create(s3_bucket="bucket-a", s3_path="data")
        self.assertEqual(b.backup_dst, "boto3+s3://bucket-a/data")
        b.write({"s3_bucket": "bucket-b"})
        self.assertEqual(b.backup_dst, "boto3+s3://bucket-b/data")

    def test_dst_updates_when_path_changes(self):
        b = self._create(s3_bucket="my-bucket", s3_path="old")
        b.write({"s3_path": "new"})
        self.assertEqual(b.backup_dst, "boto3+s3://my-bucket/new")

    def test_nested_path(self):
        b = self._create(s3_bucket="bucket", s3_path="clients/acme")
        self.assertEqual(b.backup_dst, "boto3+s3://bucket/clients/acme")


class TestCloudBackupBackendRetentionOwner(TransactionCase):
    """Imprescindible #9a — explicit retention owner + ambiguous-state alert."""

    def setUp(self):
        super().setUp()
        self.Backend = self.env["cloud.backup.backend"]
        self.Alert = self.env["cloud.alert"]

    def _create(self, **kw):
        return self.Backend.create(
            {"name": "Retention Test", "backend_type": "s3"} | kw
        )

    def _alert(self, backend):
        return self.Alert.search(
            [
                ("code", "=", "backup_retention_undeclared"),
                ("state", "=", "active"),
            ]
        ).filtered(
            lambda a: (a.payload or {}).get("backup_backend_id") == backend.id,
        )

    def test_new_backend_defaults_declared_cron(self):
        b = self._create(s3_bucket="my-bucket")
        self.assertEqual(b.retention_owner, "cron")
        self.assertTrue(b.retention_owner_declared)

    def test_write_retention_owner_marks_declared(self):
        b = self._create(s3_bucket="my-bucket", retention_owner_declared=False)
        b.write({"retention_owner": "lifecycle"})
        self.assertTrue(b.retention_owner_declared)

    def test_write_retention_owner_resolves_alert(self):
        b = self._create(s3_bucket="my-bucket", retention_owner_declared=False)
        self.Backend._cron_check_retention_declared()
        self.assertTrue(self._alert(b))
        b.write({"retention_owner": "none"})
        self.assertFalse(self._alert(b))

    def test_cron_check_retention_declared_creates_warning_for_undeclared(self):
        b = self._create(s3_bucket="my-bucket", retention_owner_declared=False)
        result = self.Backend._cron_check_retention_declared()
        self.assertEqual(result["flagged"], 1)
        alert = self._alert(b)
        self.assertTrue(alert)
        self.assertEqual(alert.level, "warning")

    def test_cron_check_retention_declared_skips_declared(self):
        self._create(s3_bucket="my-bucket")  # declared=True by default
        result = self.Backend._cron_check_retention_declared()
        self.assertEqual(result["flagged"], 0)

    def test_cron_check_retention_declared_refreshes_existing_alert(self):
        b = self._create(s3_bucket="my-bucket", retention_owner_declared=False)
        self.Backend._cron_check_retention_declared()
        first = self._alert(b)
        self.Backend._cron_check_retention_declared()
        second = self._alert(b)
        self.assertEqual(first.id, second.id)


class TestCloudBackupBackendManagedUnused(TransactionCase):
    """Imprescindible #9b — orphaned managed backend alert."""

    def setUp(self):
        super().setUp()
        self.Backend = self.env["cloud.backup.backend"]
        self.Alert = self.env["cloud.alert"]
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id",
            "0",
        )
        self.project = self.env["cloud.project"].create({"name": "MU-proj"})

    def _managed(self, **kw):
        return self.Backend.create(
            {
                "name": "Managed",
                "backend_type": "s3",
                "managed_reservation_id": "ext-123",
            }
            | kw
        )

    def _alert(self, backend):
        return self.Alert.search(
            [
                ("code", "=", "managed_backend_unused"),
                ("state", "=", "active"),
            ]
        ).filtered(
            lambda a: (a.payload or {}).get("backup_backend_id") == backend.id,
        )

    def test_unused_managed_backend_flagged(self):
        b = self._managed()
        result = self.Backend._cron_check_managed_unused()
        self.assertEqual(result["flagged"], 1)
        self.assertTrue(self._alert(b))

    def test_byo_backend_never_flagged(self):
        self.Backend.create({"name": "BYO", "backend_type": "s3"})
        result = self.Backend._cron_check_managed_unused()
        self.assertEqual(result["flagged"], 0)

    def test_managed_backend_assigned_to_instance_not_flagged(self):
        b = self._managed()
        self.env["cloud.instance"].create(
            {
                "name": "mu-inst",
                "project_id": self.project.id,
                "environment": "production",
                "backup_backend_id": b.id,
            }
        )
        result = self.Backend._cron_check_managed_unused()
        self.assertEqual(result["flagged"], 0)
        self.assertFalse(self._alert(b))

    def test_managed_backend_assigned_to_project_not_flagged(self):
        b = self._managed()
        self.project.backup_backend_id = b.id
        result = self.Backend._cron_check_managed_unused()
        self.assertEqual(result["flagged"], 0)

    def test_managed_backend_as_global_default_not_flagged(self):
        b = self._managed()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id",
            str(b.id),
        )
        result = self.Backend._cron_check_managed_unused()
        self.assertEqual(result["flagged"], 0)

    def test_assigning_backend_resolves_existing_alert(self):
        b = self._managed()
        self.Backend._cron_check_managed_unused()
        self.assertTrue(self._alert(b))
        self.env["cloud.instance"].create(
            {
                "name": "mu-inst2",
                "project_id": self.project.id,
                "environment": "production",
                "backup_backend_id": b.id,
            }
        )
        self.Backend._cron_check_managed_unused()
        self.assertFalse(self._alert(b))
