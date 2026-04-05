"""
Tier 2 — ORM integration tests for cloud.backup.backend.
Tests run inside a TransactionCase (full DB rollback after each test).
"""
from odoo.tests.common import TransactionCase


class TestCloudBackupBackendPassphrase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Backend = self.env['cloud.backup.backend']

    def _create(self, **kw):
        return self.Backend.create(
            {'name': 'Test Backend', 'backend_type': 's3'} | kw
        )

    def test_create_auto_generates_passphrase(self):
        b = self._create(s3_bucket='my-bucket')
        self.assertTrue(b.passphrase)
        self.assertGreater(len(b.passphrase), 8)

    def test_create_explicit_passphrase_preserved(self):
        b = self._create(s3_bucket='my-bucket', passphrase='explicit-pass')
        self.assertEqual(b.passphrase, 'explicit-pass')

    def test_write_empty_passphrase_keeps_existing(self):
        b = self._create(s3_bucket='my-bucket')
        original = b.passphrase
        b.write({'passphrase': ''})
        self.assertEqual(b.passphrase, original)

    def test_write_new_passphrase_updates(self):
        b = self._create(s3_bucket='my-bucket')
        b.write({'passphrase': 'new-passphrase'})
        self.assertEqual(b.passphrase, 'new-passphrase')

    def test_write_empty_secret_key_keeps_existing(self):
        b = self._create(
            s3_bucket='my-bucket',
            s3_secret_access_key='original-key',
        )
        b.write({'s3_secret_access_key': ''})
        self.assertEqual(b.s3_secret_access_key, 'original-key')

    def test_write_new_secret_key_updates(self):
        b = self._create(s3_bucket='my-bucket', s3_secret_access_key='old')
        b.write({'s3_secret_access_key': 'new-key'})
        self.assertEqual(b.s3_secret_access_key, 'new-key')


class TestCloudBackupBackendDst(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Backend = self.env['cloud.backup.backend']

    def _create(self, **kw):
        return self.Backend.create(
            {'name': 'DST Test', 'backend_type': 's3'} | kw
        )

    def test_bucket_and_path(self):
        b = self._create(s3_bucket='my-bucket', s3_path='backups')
        self.assertEqual(b.backup_dst, 'boto3+s3://my-bucket/backups')

    def test_bucket_no_path(self):
        b = self._create(s3_bucket='my-bucket', s3_path='')
        self.assertEqual(b.backup_dst, 'boto3+s3://my-bucket')

    def test_path_with_leading_trailing_slashes_stripped(self):
        b = self._create(s3_bucket='my-bucket', s3_path='/data/')
        self.assertEqual(b.backup_dst, 'boto3+s3://my-bucket/data')

    def test_no_bucket_gives_empty_dst(self):
        b = self._create()  # no s3_bucket
        self.assertEqual(b.backup_dst, '')

    def test_dst_updates_when_bucket_changes(self):
        b = self._create(s3_bucket='bucket-a', s3_path='data')
        self.assertEqual(b.backup_dst, 'boto3+s3://bucket-a/data')
        b.write({'s3_bucket': 'bucket-b'})
        self.assertEqual(b.backup_dst, 'boto3+s3://bucket-b/data')

    def test_dst_updates_when_path_changes(self):
        b = self._create(s3_bucket='my-bucket', s3_path='old')
        b.write({'s3_path': 'new'})
        self.assertEqual(b.backup_dst, 'boto3+s3://my-bucket/new')

    def test_nested_path(self):
        b = self._create(s3_bucket='bucket', s3_path='clients/acme')
        self.assertEqual(b.backup_dst, 'boto3+s3://bucket/clients/acme')
