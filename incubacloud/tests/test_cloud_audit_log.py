"""
Tier 2 — ORM integration tests for cloud.audit.log.

Covers:
  - _purge_old: guard conditions (0 / negative days), selective deletion,
    return count accuracy
  - _format: structure, empty recordset, null fields, multi-entry
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class _AuditBase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.AuditLog = self.env['cloud.audit.log']

    def _create_entry(self, action='Test Action', days_old=0, **kw):
        entry = self.AuditLog.create({'action': action} | kw)
        if days_old > 0:
            cutoff = fields.Datetime.now() - timedelta(days=days_old)
            self.env.cr.execute(
                "UPDATE cloud_audit_log SET create_date = %s WHERE id = %s",
                (cutoff, entry.id),
            )
        return entry


# ── _purge_old ────────────────────────────────────────────────────────────────

class TestAuditLogPurgeOld(_AuditBase):

    def test_zero_days_returns_zero_and_keeps_entries(self):
        old = self._create_entry(days_old=100)
        count = self.AuditLog._purge_old(0)
        self.assertEqual(count, 0)
        self.assertTrue(self.AuditLog.browse(old.id).exists())

    def test_negative_days_returns_zero(self):
        self._create_entry(days_old=100)
        count = self.AuditLog._purge_old(-5)
        self.assertEqual(count, 0)

    def test_deletes_entries_older_than_cutoff(self):
        old = self._create_entry(days_old=100)
        count = self.AuditLog._purge_old(30)
        self.assertEqual(count, 1)
        self.assertFalse(self.AuditLog.browse(old.id).exists())

    def test_keeps_entries_within_retention(self):
        recent = self._create_entry(days_old=10)
        count = self.AuditLog._purge_old(30)
        self.assertEqual(count, 0)
        self.assertTrue(self.AuditLog.browse(recent.id).exists())

    def test_selective_purge(self):
        old = self._create_entry(days_old=60)
        recent = self._create_entry(days_old=5)
        count = self.AuditLog._purge_old(30)
        self.assertEqual(count, 1)
        self.assertFalse(self.AuditLog.browse(old.id).exists())
        self.assertTrue(self.AuditLog.browse(recent.id).exists())

    def test_returns_correct_count_multiple(self):
        for _ in range(4):
            self._create_entry(days_old=100)
        for _ in range(2):
            self._create_entry(days_old=1)
        count = self.AuditLog._purge_old(30)
        self.assertEqual(count, 4)

    def test_exact_boundary_not_deleted(self):
        # Entry at exactly the cutoff boundary should NOT be deleted
        # (create_date < cutoff, not <=)
        boundary = self._create_entry()
        exactly_at = fields.Datetime.now() - timedelta(days=30)
        self.env.cr.execute(
            "UPDATE cloud_audit_log SET create_date = %s WHERE id = %s",
            (exactly_at, boundary.id),
        )
        count = self.AuditLog._purge_old(30)
        self.assertEqual(count, 0)
        self.assertTrue(self.AuditLog.browse(boundary.id).exists())

    def test_purge_empty_table_returns_zero(self):
        count = self.AuditLog._purge_old(30)
        self.assertEqual(count, 0)

    def test_string_days_accepted(self):
        # _purge_old does int(days) internally
        old = self._create_entry(days_old=100)
        count = self.AuditLog._purge_old('30')
        self.assertEqual(count, 1)
        self.assertFalse(self.AuditLog.browse(old.id).exists())


# ── _format ───────────────────────────────────────────────────────────────────

class TestAuditLogFormat(_AuditBase):

    def test_empty_recordset_returns_empty_list(self):
        self.assertEqual(self.AuditLog._format(), [])

    def test_returns_list_of_dicts(self):
        result = self._create_entry()._format()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], dict)

    def test_has_all_required_keys(self):
        entry = self._create_entry()
        item = entry._format()[0]
        for key in ('id', 'user_name', 'action', 'details', 'create_date', 'job_id'):
            self.assertIn(key, item, f"Missing key: {key}")

    def test_id_matches(self):
        entry = self._create_entry()
        item = entry._format()[0]
        self.assertEqual(item['id'], entry.id)

    def test_action_matches(self):
        entry = self._create_entry(action='Rebuild Instance')
        item = entry._format()[0]
        self.assertEqual(item['action'], 'Rebuild Instance')

    def test_details_empty_string_when_false(self):
        entry = self._create_entry()
        item = entry._format()[0]
        self.assertEqual(item['details'], '')

    def test_details_preserved_when_set(self):
        entry = self.AuditLog.create({
            'action': 'Test',
            'details': 'some detail',
        })
        item = entry._format()[0]
        self.assertEqual(item['details'], 'some detail')

    def test_job_id_none_when_no_job(self):
        entry = self._create_entry()
        item = entry._format()[0]
        self.assertIsNone(item['job_id'])

    def test_user_name_is_string(self):
        entry = self._create_entry()
        item = entry._format()[0]
        self.assertIsInstance(item['user_name'], str)
        self.assertTrue(item['user_name'])  # Should have a name (current user)

    def test_create_date_present(self):
        entry = self._create_entry()
        item = entry._format()[0]
        self.assertIsNotNone(item['create_date'])

    def test_multiple_entries_returns_all(self):
        entries = self.AuditLog
        for i in range(3):
            entries |= self._create_entry(action=f'Action {i}')
        result = entries._format()
        self.assertEqual(len(result), 3)
        actions = {r['action'] for r in result}
        self.assertEqual(actions, {'Action 0', 'Action 1', 'Action 2'})

    def test_with_host_and_instance_linked(self):
        host = self.env['cloud.host'].create({
            'name': 'h', 'ip_address': '10.0.0.1',
            'user': 'u', 'wildcard_domain': 'example.com',
        })
        project = self.env['cloud.project'].create({'name': 'p'})
        instance = self.env['cloud.instance'].create({
            'name': 'inst', 'project_id': project.id, 'host_id': host.id,
        })
        entry = self.AuditLog.create({
            'action': 'Deploy',
            'host_id': host.id,
            'instance_id': instance.id,
        })
        item = entry._format()[0]
        self.assertEqual(item['action'], 'Deploy')
        self.assertIsNone(item['job_id'])
