"""Every alert goes through one engine, with one idea of identity.

Fourteen producers created their alerts by hand, each with its own search
for an open one: no confirmation policy, no new notice when a warning turned
critical, and identities that drifted from the engine's (19-oct-2026). They
now all call ``raise_alert``, which learned what they needed: a key for
what the target alone does not tell apart (two backup backends, two types of
failed job), a project for an alert about a whole project, and an instance
alert that stays the same alert when the instance moves to another host.
"""
import importlib.util

import psycopg2

from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger
from odoo.tools.misc import file_path

_CODE = 'identity_test'


class TestAlertIdentity(TransactionCase):

    def setUp(self):
        super().setUp()
        Host = self.env['cloud.host']
        common = {'user': 'ubuntu', 'wildcard_domain': 'identity.example.com'}
        self.host_a = Host.create({'name': 'id-a', 'ip_address': '192.0.2.81', **common})
        self.host_b = Host.create({'name': 'id-b', 'ip_address': '192.0.2.82', **common})
        self.project = self.env['cloud.project'].create({'name': 'Identity'})
        self.other_project = self.env['cloud.project'].create({'name': 'Identity 2'})
        self.inst = self.env['cloud.instance'].create({
            'name': 'identityinst',
            'project_id': self.project.id,
            'environment': 'production',
            'host_id': self.host_a.id,
        })
        self.Alert = self.env['cloud.alert']

    def test_an_instance_alert_stays_one_alert_when_it_moves(self):
        first = self.Alert.raise_alert(_CODE, 'on a', host=self.host_a, instance=self.inst)
        again = self.Alert.raise_alert(_CODE, 'on b', host=self.host_b, instance=self.inst)
        self.assertEqual(again, first)
        self.assertEqual(first.host_id, self.host_b)
        self.assertEqual(first.message, 'on b')
        self.assertEqual(
            self.Alert.resolve_alert(_CODE, instance=self.inst), first,
        )

    def test_a_host_alert_is_not_an_instance_alert(self):
        on_host = self.Alert.raise_alert(_CODE, 'host', host=self.host_a)
        on_instance = self.Alert.raise_alert(
            _CODE, 'instance', host=self.host_a, instance=self.inst,
        )
        self.assertNotEqual(on_host, on_instance)

    def test_a_key_tells_two_alerts_on_one_target_apart(self):
        one = self.Alert.raise_alert(_CODE, '1', key='backup_backend:1')
        two = self.Alert.raise_alert(_CODE, '2', key='backup_backend:2')
        self.assertNotEqual(one, two)
        self.assertEqual(self.Alert.raise_alert(_CODE, '1b', key='backup_backend:1'), one)
        self.Alert.resolve_alert(_CODE, key='backup_backend:2')
        self.assertEqual((one.state, two.state), ('active', 'dismissed'))

    def test_a_project_alert_is_one_per_project(self):
        target = self.Alert._link_target('project_id', self.project.id)
        mine = self.Alert.raise_alert(_CODE, 'mine', **target)
        theirs = self.Alert.raise_alert(
            _CODE, 'theirs',
            **self.Alert._link_target('project_id', self.other_project.id),
        )
        self.assertNotEqual(mine, theirs)
        self.assertEqual(mine.project_id, self.project)
        self.assertEqual(self.Alert.raise_alert(_CODE, 'mine again', **target), mine)

    def test_extra_fields_are_written_on_creation_and_refresh(self):
        alert = self.Alert.raise_alert(
            _CODE, 'x', instance=self.inst, extra={'conflict_data': [{'name': 'a'}]},
        )
        self.Alert.raise_alert(
            _CODE, 'y', instance=self.inst, extra={'conflict_data': [{'name': 'b'}]},
        )
        self.assertEqual(alert.conflict_data, [{'name': 'b'}])

    def test_the_database_refuses_twins_of_one_identity(self):
        """Producers racing under snapshot isolation cannot see each other."""
        vals = {
            'code': _CODE, 'message': 'twin', 'instance_id': self.inst.id,
            'dedup_key': 'k',
        }
        self.Alert.create({**vals, 'host_id': self.host_a.id})
        with mute_logger('odoo.sql_db'), self.assertRaises(psycopg2.IntegrityError), \
                self.env.cr.savepoint():
            self.Alert.create({**vals, 'host_id': self.host_b.id})


class TestNotificationDefault(TransactionCase):

    def test_a_new_user_hears_of_warnings(self):
        """A failed backup archive or a declined payment reached nobody."""
        defaults = self.env['res.users'].default_get(['cloud_notification_level'])
        self.assertEqual(defaults['cloud_notification_level'], 'failures_warnings')


class TestOpenAlertsTakeTheirKey(TransactionCase):
    """The 1.0.193 upgrade gives open alerts the identity raise_alert uses."""

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            'post_migrate_1_0_192',
            file_path('incubacloud/migrations/1.0.193/post-migrate.py'),
        )
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        self.host = self.env['cloud.host'].create({
            'name': 'mig-host', 'ip_address': '192.0.2.83', 'user': 'ubuntu',
            'wildcard_domain': 'mig.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'Migration'})
        self.Alert = self.env['cloud.alert']

    def _alert(self, code, **vals):
        """An open alert written as the producers did before 1.0.193."""
        return self.Alert.create({'code': code, 'message': code} | vals)

    def test_each_kind_gets_its_key_and_twins_are_dismissed(self):
        old_twin = self._alert(
            'managed_backend_unused', payload={'backup_backend_id': 7},
        )
        backend_7 = self._alert(
            'managed_backend_unused', payload={'backup_backend_id': 7},
        )
        backend_8 = self._alert(
            'managed_backend_unused', payload={'backup_backend_id': 8},
        )
        job_type = self.env['cloud.job.type'].search([], limit=1)
        job = self.env['cloud.job'].create({
            'name': 'mig job', 'host_id': self.host.id, 'job_type_id': job_type.id,
        })
        failed = self._alert('job_failed', host_id=self.host.id, job_id=job.id)
        project_only = self._alert('pip_conflict', project_id=self.project.id)

        self.migration.migrate(self.env.cr, '1.0.191')
        self.env.invalidate_all()

        self.assertEqual(old_twin.state, 'dismissed')
        self.assertEqual(backend_7.dedup_key, 'backup_backend:7')
        self.assertEqual(backend_8.dedup_key, 'backup_backend:8')
        self.assertEqual(failed.dedup_key, f'job_type:{job_type.code}')
        self.assertEqual(project_only.dedup_key, f'project:{self.project.id}')
        # And the engine finds them where it looks.
        self.assertEqual(
            self.Alert.raise_alert(
                'managed_backend_unused', 'again', key='backup_backend:7',
            ),
            backend_7,
        )
