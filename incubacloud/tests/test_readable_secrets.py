"""An alert on a secret that will not decrypt closes once it reads again.

Nothing closed them: the right key installed and the panel restarted,
the panels worked and their alerts stayed lit until dismissed by hand
(18-oct-2026). ``cloud.settings._cron_resolve_readable_secrets`` reads
the values again, hourly and at startup, and closes what opens.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_alert import CloudAlert
from odoo.addons.incubacloud.models.encrypted_char import (
    ALERT_CODE_UNREADABLE,
    create_unreadable_alert,
)

# A Fernet token no key of ours can open.
_UNOPENABLE = 'enc:gAAAAABmb3Jn3WQtY2lwaGVydGV4dC10aGF0LW5vLWtleQ=='


class TestUnreadableSecretAlertsCloseThemselves(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'rs-host', 'ip_address': '10.9.8.7', 'user': 'ubuntu',
            'wildcard_domain': 'rs.example.com', 'password': 'a-secret',
        })
        self.settings = self.env['cloud.settings']
        self.notified = patch.object(
            CloudAlert, '_notify_alert_resolved', autospec=True,
        )

    def _corrupt(self, record, field):
        """Put a token no key opens in *record.field*, by SQL."""
        self.env.cr.execute(
            f'UPDATE {record._table} SET {field} = %s WHERE id = %s',
            (_UNOPENABLE, record.id),
        )
        record.invalidate_recordset([field])

    def _sweep(self):
        with self.notified as notify:
            closed = self.settings._cron_resolve_readable_secrets()
        return closed, notify

    def test_an_alert_on_a_value_that_reads_again_is_closed(self):
        alert = create_unreadable_alert(self.env, self.host, 'password')
        closed, notify = self._sweep()
        self.assertGreaterEqual(closed, 1)
        self.assertEqual(alert.state, 'dismissed')
        self.assertEqual(alert.resolution, 'auto')
        notify.assert_any_call(alert)

    def test_a_value_still_unreadable_keeps_its_alert(self):
        alert = create_unreadable_alert(self.env, self.host, 'password')
        self._corrupt(self.host, 'password')
        self._sweep()
        self.assertEqual(alert.state, 'active')

    def test_an_instance_secret_is_read_from_its_own_row(self):
        project = self.env['cloud.project'].create({'name': 'rs-proj'})
        inst = self.env['cloud.instance'].create({
            'name': 'rs-inst', 'project_id': project.id,
            'host_id': self.host.id,
        })
        alert = create_unreadable_alert(
            self.env, inst, 'odoo_admin_password',
        )
        self.assertEqual(alert.instance_id, inst)
        self._corrupt(inst, 'odoo_admin_password')
        self._sweep()
        self.assertEqual(alert.state, 'active')
        inst.odoo_admin_password = 'set-again'
        self.env.flush_all()
        self._sweep()
        self.assertEqual(alert.state, 'dismissed')

    def test_an_alert_on_a_linked_model_covers_every_row_of_its_target(self):
        """An alert on a job's secret hangs on the job's host and dedups
        there: it stands for every job of that host, and closes only once
        none of them holds an unreadable value."""
        Job = self.env['cloud.job']
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'host_probe')], limit=1,
        )
        jobs = Job.create([
            {'name': f'rs-job-{n}', 'host_id': self.host.id,
             'job_type_id': job_type.id, 'secret_payload': 'x'}
            for n in range(2)
        ])
        alert = create_unreadable_alert(self.env, jobs[0], 'secret_payload')
        self.assertEqual(alert.host_id, self.host)
        self._corrupt(jobs[1], 'secret_payload')
        self._sweep()
        self.assertEqual(alert.state, 'active')
        jobs[1].unlink()
        self._sweep()
        self.assertEqual(alert.state, 'dismissed')

    def test_a_stranded_rotation_closes_the_same_way(self):
        alert = self.env['cloud.alert'].create({
            'code': 'secret_rotation_stranded:cloud.host.password',
            'level': 'warning', 'host_id': self.host.id, 'message': 'm',
        })
        self._sweep()
        self.assertEqual(alert.state, 'dismissed')

    def test_an_alert_naming_no_encrypted_field_is_left_alone(self):
        alert = self.env['cloud.alert'].create({
            'code': f'{ALERT_CODE_UNREADABLE}:cloud.host.name',
            'level': 'critical', 'host_id': self.host.id, 'message': 'm',
        })
        self._sweep()
        self.assertEqual(alert.state, 'active')

    def test_the_pass_is_scheduled_hourly(self):
        cron = self.env.ref('incubacloud.cron_resolve_readable_secrets')
        self.assertTrue(cron.active)
        self.assertEqual(
            (cron.interval_number, cron.interval_type), (1, 'hours'),
        )
