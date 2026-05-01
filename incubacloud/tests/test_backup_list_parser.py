"""
Tier 1 — Pure-Python unit tests for backup list parsing.

Tests _parse_collection_status and _parse_duplicity_date from
backup_list_executor, plus cloud.instance.backup model sync.
"""
import unittest


from odoo.tests.common import TransactionCase, BaseCase


# ── Tier 1: Pure parser tests ────────────────────────────────────────────────

class TestParseDuplicityDate(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.backup_list_executor import (
            _parse_duplicity_date,
        )
        self.parse = _parse_duplicity_date

    def test_standard_format(self):
        result = self.parse('Thu Mar 19 16:50:04 2026')
        self.assertIn('2026-03-19', result)
        self.assertIn('16:50:04', result)

    def test_double_space_day(self):
        """Single-digit days sometimes have two spaces."""
        result = self.parse('Mon Jan  5 09:00:00 2026')
        self.assertIn('2026-01-05', result)

    def test_unparseable_returned_as_is(self):
        result = self.parse('not a date')
        self.assertEqual(result, 'not a date')


class TestParseCollectionStatus(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.backup_list_executor import (
            _parse_collection_status,
        )
        self.parse = _parse_collection_status

    def test_no_backups(self):
        output = (
            "Last full backup date: none\n"
            "Collection Status\n"
            "-----------------\n"
            "No backup chains with active signatures found\n"
        )
        result = self.parse(output)
        self.assertEqual(result['total_chains'], 0)
        self.assertEqual(result['chains'], [])

    def test_single_full_backup(self):
        output = (
            "Last full backup date: Thu Mar 19 16:50:04 2026\n"
            "Collection Status\n"
            "-----------------\n"
            "Found primary backup chain with matching signature chain:\n"
            "-------------------------\n"
            "Chain start time: Thu Mar 19 16:50:04 2026\n"
            "Chain end time: Thu Mar 19 16:50:04 2026\n"
            "Number of contained backup sets: 1\n"
            "Total number of contained volumes: 1\n"
            " Type of backup set:                            Time:      "
            "Num volumes:\n"
            "                Full         Thu Mar 19 16:50:04 2026"
            "                 1\n"
            "-------------------------\n"
        )
        result = self.parse(output)
        self.assertEqual(result['total_chains'], 1)
        chain = result['chains'][0]
        self.assertTrue(chain['is_primary'])
        self.assertEqual(len(chain['backups']), 1)
        bk = chain['backups'][0]
        self.assertEqual(bk['type'], 'Full')
        self.assertEqual(bk['volumes'], 1)
        self.assertIn('2026-03-19', bk['time'])

    def test_full_plus_incremental(self):
        output = (
            "Found primary backup chain with matching signature chain:\n"
            "Chain start time: Thu Mar 19 16:50:04 2026\n"
            "Chain end time: Fri Mar 20 02:00:04 2026\n"
            "Number of contained backup sets: 2\n"
            "Total number of contained volumes: 2\n"
            " Type of backup set:                            Time:      "
            "Num volumes:\n"
            "                Full         Thu Mar 19 16:50:04 2026"
            "                 1\n"
            "         Incremental         Fri Mar 20 02:00:04 2026"
            "                 1\n"
        )
        result = self.parse(output)
        chain = result['chains'][0]
        self.assertEqual(len(chain['backups']), 2)
        self.assertEqual(chain['backups'][0]['type'], 'Full')
        self.assertEqual(chain['backups'][1]['type'], 'Incremental')

    def test_secondary_chain_not_primary(self):
        output = (
            "Found 1 secondary backup chain(s).\n"
            "Chain start time: Mon Mar 10 00:00:00 2026\n"
            "Chain end time: Mon Mar 10 00:00:00 2026\n"
            "Number of contained backup sets: 1\n"
            "Total number of contained volumes: 1\n"
            " Type of backup set:                            Time:      "
            "Num volumes:\n"
            "                Full         Mon Mar 10 00:00:00 2026"
            "                 1\n"
            "Found primary backup chain with matching signature chain:\n"
            "Chain start time: Thu Mar 19 16:50:04 2026\n"
            "Chain end time: Thu Mar 19 16:50:04 2026\n"
            "Number of contained backup sets: 1\n"
            "Total number of contained volumes: 1\n"
            " Type of backup set:                            Time:      "
            "Num volumes:\n"
            "                Full         Thu Mar 19 16:50:04 2026"
            "                 1\n"
        )
        result = self.parse(output)
        self.assertEqual(result['total_chains'], 2)
        self.assertFalse(result['chains'][0]['is_primary'])
        self.assertTrue(result['chains'][1]['is_primary'])

    def test_empty_output(self):
        result = self.parse('')
        self.assertEqual(result['total_chains'], 0)


# ── Tier 2: ORM sync tests ──────────────────────────────────────────────────

class TestBackupRecordSync(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'BkProj'})
        self.inst = self.env['cloud.instance'].create({
            'name': 'bk-test',
            'project_id': self.project.id,
            'environment': 'production',
        })
        self.Backup = self.env['cloud.instance.backup']

    def _create_backup(self, **kw):
        base = {
            'instance_id': self.inst.id,
            'backup_type': 'Full',
            'backup_time': '2026-03-19 16:50:04',
        }
        return self.Backup.create(base | kw)

    def test_create_backup_record(self):
        bk = self._create_backup()
        self.assertTrue(bk.id)
        self.assertEqual(bk.backup_type, 'Full')

    def test_cascade_delete(self):
        bk = self._create_backup()
        bk_id = bk.id
        self.inst.unlink()
        self.assertFalse(self.Backup.browse(bk_id).exists())

    def test_ordering_by_time_desc(self):
        b1 = self._create_backup(backup_time='2026-03-18 10:00:00')
        b2 = self._create_backup(backup_time='2026-03-20 10:00:00')
        b3 = self._create_backup(backup_time='2026-03-19 10:00:00')
        backups = self.Backup.search([('instance_id', '=', self.inst.id)])
        self.assertEqual(backups[0], b2)
        self.assertEqual(backups[1], b3)
        self.assertEqual(backups[2], b1)

    def test_incremental_type(self):
        bk = self._create_backup(backup_type='Incremental')
        self.assertEqual(bk.backup_type, 'Incremental')

    def test_chain_start_stored(self):
        bk = self._create_backup(chain_start='2026-03-15 00:00:00')
        self.assertTrue(bk.chain_start)

    def test_primary_flag(self):
        bk = self._create_backup(is_primary=True)
        self.assertTrue(bk.is_primary)

    def test_attachment_id_optional(self):
        """attachment_id defaults to empty (production backups have no attachment)."""
        bk = self._create_backup()
        self.assertFalse(bk.attachment_id)

    def test_attachment_id_stored(self):
        """Non-prod backups can link an ir.attachment."""
        att = self.env['ir.attachment'].create({
            'name': 'bk-test-backup-20260407T120000.zip',
            'type': 'binary',
            'datas': 'dGVzdA==',  # b64 for "test"
            'res_model': 'cloud.job',
            'res_id': 0,
        })
        bk = self._create_backup(attachment_id=att.id)
        self.assertEqual(bk.attachment_id, att)

    def test_attachment_id_set_null_on_attachment_delete(self):
        """Deleting the attachment sets attachment_id to False (ondelete=set null)."""
        att = self.env['ir.attachment'].create({
            'name': 'bk-test-backup-20260407T120001.zip',
            'type': 'binary',
            'datas': 'dGVzdA==',
            'res_model': 'cloud.job',
            'res_id': 0,
        })
        bk = self._create_backup(attachment_id=att.id)
        att.unlink()
        bk.invalidate_recordset()
        self.assertFalse(bk.attachment_id)


class TestNonProdCreateBackupChain(TransactionCase):
    """create_backup always enqueues backup_create → backup_list chain."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'H1', 'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'P'})

    def _staging_inst(self):
        return self.env['cloud.instance'].create({
            'name': 'staging',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
            'deployed': True,
        })

    def test_create_backup_enqueues_two_jobs(self):
        inst = self._staging_inst()
        polled_id = inst.create_backup()
        jobs = self.env['cloud.job'].search(
            [('instance_id', '=', inst.id)],
            order='id asc',
        )
        codes = [j.job_type_id.code for j in jobs]
        self.assertIn('backup_create', codes)
        self.assertIn('backup_list', codes)
        # create_backup returns the trailing backup_list id so the UI
        # only sees state='done' once duplicity records are synced.
        self.assertEqual(jobs[-1].id, polled_id)
        self.assertEqual(jobs[-1].job_type_id.code, 'backup_list')

    def test_list_backups_non_prod_enqueues_one_job(self):
        inst = self._staging_inst()
        inst.list_backups()
        jobs = self.env['cloud.job'].search(
            [('instance_id', '=', inst.id)],
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_type_id.code, 'backup_list')
