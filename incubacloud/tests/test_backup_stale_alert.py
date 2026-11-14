"""``backup_stale``: a production instance whose scheduled backups stopped.

The daily ``backup_list`` of a production instance mirrors the duplicity
sets; when the newest is older than ``BACKUP_STALE_HOURS`` it raises the
alert, and the first listing that sees a fresh set closes it. Nothing else
told the operator that the copies had stopped (14-nov-2026).
"""
import asyncio
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.backup_list_executor import (
    BACKUP_STALE_ALERT_CODE,
    BACKUP_STALE_HOURS,
    BackupListExecutor,
)
from odoo.addons.incubacloud.models.cloud_instance import CloudInstance


class TestBackupStaleAlert(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'stale-host', 'ip_address': '198.51.100.71',
            'user': 'root', 'wildcard_domain': 'stale.example',
        })
        backend = self.env['cloud.backup.backend'].create({
            'name': 'stale-s3', 'backend_type': 's3', 's3_bucket': 'bucket',
        })
        project = self.env['cloud.project'].create({'name': 'StaleProj'})
        self.inst = self.env['cloud.instance'].create({
            'name': 'stale-inst', 'project_id': project.id,
            'host_id': self.host.id, 'environment': 'production',
            'backup_backend_id': backend.id, 'state': 'deployed',
            'running': True, 'compose_services': 'odoo,db,backup',
        })
        job = self.env['cloud.job'].create({
            'name': 'backup_list', 'host_id': self.host.id,
            'instance_id': self.inst.id,
            'job_type_id': self.env.ref('incubacloud.backup_list').id,
        })
        self.executor = BackupListExecutor(job, self.host)

    def _set(self, hours_ago, kind='chain'):
        """Record a backup set taken *hours_ago*."""
        self.env['cloud.instance.backup'].create({
            'instance_id': self.inst.id, 'backup_type': 'Full', 'kind': kind,
            'backup_time': fields.Datetime.now() - timedelta(hours=hours_ago),
        })

    def _created(self, hours_ago):
        """Backdate the instance's creation by *hours_ago*."""
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE cloud_instance SET create_date = %s WHERE id = %s',
            (fields.Datetime.now() - timedelta(hours=hours_ago), self.inst.id),
        )
        self.inst.invalidate_recordset(['create_date'])

    def _alert(self):
        """Return the active ``backup_stale`` alert of the instance."""
        return self.env['cloud.alert'].search([
            ('code', '=', BACKUP_STALE_ALERT_CODE),
            ('instance_id', '=', self.inst.id),
            ('state', '=', 'active'),
        ])

    def test_old_newest_set_raises(self):
        self._set(BACKUP_STALE_HOURS + 1)
        self.executor._check_backup_freshness()
        self.assertEqual(len(self._alert()), 1)

    def test_fresh_set_closes_it(self):
        self._set(BACKUP_STALE_HOURS + 30)
        self.executor._check_backup_freshness()
        self.assertTrue(self._alert())
        self._set(2)
        self.executor._check_backup_freshness()
        self.assertFalse(self._alert())

    def test_recent_set_raises_nothing(self):
        self._set(BACKUP_STALE_HOURS - 1)
        self.executor._check_backup_freshness()
        self.assertFalse(self._alert())

    def test_new_instance_without_sets_waits(self):
        self.executor._check_backup_freshness()
        self.assertFalse(self._alert())

    def test_old_instance_without_sets_raises(self):
        self._created(BACKUP_STALE_HOURS + 5)
        self.executor._check_backup_freshness()
        self.assertTrue(self._alert())

    def test_a_manual_archive_is_not_a_scheduled_backup(self):
        self._set(BACKUP_STALE_HOURS + 10)
        self._set(1, kind='archive')
        self.executor._check_backup_freshness()
        self.assertTrue(self._alert())

    def test_listing_runs_the_check(self):
        """``on_success`` of a production listing raises it."""
        self._created(BACKUP_STALE_HOURS + 5)
        with patch.object(
            BackupListExecutor, '_populate_sizes_from_s3', autospec=True,
        ):
            asyncio.run(self.executor.on_success({
                'List backups': {'stdout': '', 'exit_status': 0},
            }))
        self.assertTrue(self._alert())

    def test_cron_closes_it_once_backups_are_no_longer_scheduled(self):
        self._set(BACKUP_STALE_HOURS + 10)
        self.executor._check_backup_freshness()
        self.assertTrue(self._alert())
        self.inst.compose_services = 'odoo,db'
        with patch.object(CloudInstance, 'list_backups', autospec=True) as listed:
            self.env['cloud.instance'].cron_refresh_backup_list()
        listed.assert_not_called()
        self.assertFalse(self._alert())
