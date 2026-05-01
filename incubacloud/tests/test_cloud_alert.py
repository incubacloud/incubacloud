"""
Tier 2 — ORM integration tests for cloud.alert.

Tests cover:
  - host/instance/project target combinations (all optional — global
    alerts with no target are intentional, gated by record rules)
  - instance_id relation on cloud.instance.alert_ids
  - cascade delete behaviour
"""
from odoo.tests.common import TransactionCase


class TestCloudAlertConstraint(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Alert Test Host',
            'ip_address': '10.0.0.2',
            'user': 'ubuntu', 'wildcard_domain': 'test.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'Alert Project'})
        self.inst = self.env['cloud.instance'].create({
            'name': 'alert-inst',
            'project_id': self.project.id,
            'environment': 'staging',
        })

    def _alert(self, **kw):
        base = {'code': 'test_code', 'message': 'test message', 'level': 'warning'}
        return self.env['cloud.alert'].create(base | kw)

    def test_host_only_is_valid(self):
        alert = self._alert(host_id=self.host.id)
        self.assertTrue(alert.id)

    def test_instance_only_is_valid(self):
        alert = self._alert(instance_id=self.inst.id)
        self.assertTrue(alert.id)

    def test_both_host_and_instance_is_valid(self):
        alert = self._alert(host_id=self.host.id, instance_id=self.inst.id)
        self.assertTrue(alert.id)

    def test_neither_target_is_allowed(self):
        """Targetless alerts are valid — they signal global/platform
        events (OIDC code reuse, JWKS overdue rotation, etc.). Visibility
        is restricted at the record-rule layer to project-managers+."""
        alert = self._alert()
        self.assertTrue(alert.id)
        self.assertFalse(alert.host_id)
        self.assertFalse(alert.instance_id)

    def test_default_state_is_active(self):
        alert = self._alert(host_id=self.host.id)
        self.assertEqual(alert.state, 'active')

    def test_default_level_is_warning(self):
        alert = self._alert(host_id=self.host.id)
        self.assertEqual(alert.level, 'warning')

    def test_critical_level_accepted(self):
        alert = self._alert(host_id=self.host.id, level='critical')
        self.assertEqual(alert.level, 'critical')


class TestCloudAlertInstanceScoped(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'Inst Alert Project'})
        self.inst = self.env['cloud.instance'].create({
            'name': 'health-inst',
            'project_id': self.project.id,
            'environment': 'staging',
        })

    def _inst_alert(self, code='inst_code', **kw):
        base = {'code': code, 'message': 'msg', 'level': 'warning',
                'instance_id': self.inst.id}
        return self.env['cloud.alert'].create(base | kw)

    def test_alert_ids_relation(self):
        alert = self._inst_alert()
        self.assertIn(alert, self.inst.alert_ids)

    def test_multiple_alerts_in_relation(self):
        a1 = self._inst_alert(code='code_1')
        a2 = self._inst_alert(code='code_2')
        self.assertIn(a1, self.inst.alert_ids)
        self.assertIn(a2, self.inst.alert_ids)

    def test_cascade_delete_on_instance_unlink(self):
        alert_id = self._inst_alert().id
        self.inst.unlink()
        remaining = self.env['cloud.alert'].search([('id', '=', alert_id)])
        self.assertFalse(remaining)

    def test_dismiss_state_change(self):
        alert = self._inst_alert()
        alert.write({'state': 'dismissed'})
        self.assertEqual(alert.state, 'dismissed')

    def test_alerts_from_other_instances_not_in_relation(self):
        other_inst = self.env['cloud.instance'].create({
            'name': 'other-inst',
            'project_id': self.project.id,
            'environment': 'staging',
        })
        other_alert = self.env['cloud.alert'].create({
            'code': 'other_code', 'message': 'other', 'level': 'warning',
            'instance_id': other_inst.id,
        })
        self.assertNotIn(other_alert, self.inst.alert_ids)


class TestCloudAlertProjectAndConflictData(TransactionCase):
    """cloud.alert supports project_id target and conflict_data JSON field."""

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'Conflict Data Project'})

    def _alert(self, **kw):
        base = {'code': 'pip_conflict', 'message': 'msg', 'level': 'warning'}
        return self.env['cloud.alert'].create(base | kw)

    def test_project_only_alert_passes_constraint(self):
        alert = self._alert(project_id=self.project.id)
        self.assertTrue(alert.id)

    def test_targetless_alert_with_conflict_data_is_allowed(self):
        """Targetless alerts (e.g. system-wide pip conflicts spotted by
        a cron) may still carry conflict_data; record rules restrict
        who sees them, the model itself imposes no target requirement."""
        data = [{'name': 'lib', 'existing': 'lib>=1', 'incoming': 'lib<1'}]
        alert = self._alert(conflict_data=data)
        self.assertTrue(alert.id)
        self.assertEqual(alert.conflict_data, data)

    def test_conflict_data_roundtrip(self):
        data = [{'name': 'lib1', 'existing': 'lib1>=3.0', 'incoming': 'lib1<3.0'}]
        alert = self._alert(project_id=self.project.id, conflict_data=data)
        self.assertEqual(alert.conflict_data, data)

    def test_conflict_data_default_is_none(self):
        inst = self.env['cloud.instance'].create({
            'name': 'cd-inst',
            'project_id': self.project.id,
            'environment': 'staging',
        })
        alert = self._alert(instance_id=inst.id)
        self.assertFalse(alert.conflict_data)
