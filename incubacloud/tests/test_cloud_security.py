"""
Tier 2 -- ORM integration tests for the role-based access control system.

Tests the cloud.security.mixin helpers, model-level create/write/unlink
guards, and ir.rule record rules for project-scoped visibility.
"""
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged


class CloudSecurityBase(TransactionCase):
    """Shared setUp for all security test classes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Workaround: optional modules may add NOT NULL columns to
        # res_partner without defaults.  new_test_user() creates a
        # partner without those fields → constraint violation.
        # Patch any such column with a safe SQL default so our tests
        # don't need to know which modules are installed.
        cls.env.cr.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'res_partner'
               AND is_nullable = 'NO'
               AND column_default IS NULL
               AND column_name NOT IN ('id', 'name', 'company_type',
                                       'type', 'lang', 'active',
                                       'create_uid', 'write_uid',
                                       'create_date', 'write_date')
        """)
        for col, dtype in cls.env.cr.fetchall():
            default = "''" if 'char' in dtype or 'text' in dtype else "'no'"
            cls.env.cr.execute(
                f'ALTER TABLE res_partner '
                f'ALTER COLUMN "{col}" SET DEFAULT {default}'
            )
        cls.group_stakeholder = cls.env.ref('incubacloud.group_cloud_user')
        cls.group_consultant = cls.env.ref('incubacloud.group_cloud_consultant')
        cls.group_pm = cls.env.ref('incubacloud.group_cloud_project_manager')
        cls.group_developer = cls.env.ref('incubacloud.group_cloud_developer')
        cls.group_manager = cls.env.ref('incubacloud.group_cloud_manager')

    def _create_user(self, login, group_xmlid):
        return new_test_user(
            self.env, login=login,
            groups=f'base.group_user,incubacloud.{group_xmlid}',
        )

    def _create_project(self, name='Test Project', members=None):
        vals = {'name': name}
        if members:
            vals['member_ids'] = [(4, u.id) for u in members]
        return self.env['cloud.project'].create(vals)

    def _create_instance(self, project, environment='staging', **kw):
        return self.env['cloud.instance'].create({
            'name': f'inst-{environment}',
            'project_id': project.id,
            'environment': environment,
        } | kw)


# ── 1. Mixin tests ──────────────────────────────────────────────────────


class TestCloudSecurityMixin(CloudSecurityBase):
    """Tests for _check_cloud_group, _has_cloud_group, _get_user_role,
    and _get_user_permissions on cloud.security.mixin."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('sec_stakeholder', 'group_cloud_user')
        self.consultant = self._create_user('sec_consultant', 'group_cloud_consultant')
        self.pm = self._create_user('sec_pm', 'group_cloud_project_manager')
        self.developer = self._create_user('sec_developer', 'group_cloud_developer')
        self.manager = self._create_user('sec_manager', 'group_cloud_manager')

    # -- _check_cloud_group: raises for insufficient role ----------------

    def test_check_cloud_group_stakeholder_cannot_reach_consultant(self):
        mixin = self.env['cloud.project'].with_user(self.stakeholder)
        with self.assertRaises(AccessError):
            mixin._check_cloud_group('group_cloud_consultant')

    def test_check_cloud_group_consultant_cannot_reach_pm(self):
        mixin = self.env['cloud.project'].with_user(self.consultant)
        with self.assertRaises(AccessError):
            mixin._check_cloud_group('group_cloud_project_manager')

    def test_check_cloud_group_pm_cannot_reach_developer(self):
        mixin = self.env['cloud.project'].with_user(self.pm)
        with self.assertRaises(AccessError):
            mixin._check_cloud_group('group_cloud_developer')

    def test_check_cloud_group_developer_cannot_reach_manager(self):
        mixin = self.env['cloud.project'].with_user(self.developer)
        with self.assertRaises(AccessError):
            mixin._check_cloud_group('group_cloud_manager')

    # -- _check_cloud_group: passes for exact role ----------------------

    def test_check_cloud_group_exact_stakeholder(self):
        mixin = self.env['cloud.project'].with_user(self.stakeholder)
        self.assertTrue(mixin._check_cloud_group('group_cloud_user'))

    def test_check_cloud_group_exact_consultant(self):
        mixin = self.env['cloud.project'].with_user(self.consultant)
        self.assertTrue(mixin._check_cloud_group('group_cloud_consultant'))

    def test_check_cloud_group_exact_pm(self):
        mixin = self.env['cloud.project'].with_user(self.pm)
        self.assertTrue(mixin._check_cloud_group('group_cloud_project_manager'))

    def test_check_cloud_group_exact_developer(self):
        mixin = self.env['cloud.project'].with_user(self.developer)
        self.assertTrue(mixin._check_cloud_group('group_cloud_developer'))

    def test_check_cloud_group_exact_manager(self):
        mixin = self.env['cloud.project'].with_user(self.manager)
        self.assertTrue(mixin._check_cloud_group('group_cloud_manager'))

    # -- _check_cloud_group: passes for higher role ---------------------

    def test_check_cloud_group_manager_passes_for_consultant(self):
        mixin = self.env['cloud.project'].with_user(self.manager)
        self.assertTrue(mixin._check_cloud_group('group_cloud_consultant'))

    def test_check_cloud_group_developer_passes_for_stakeholder(self):
        mixin = self.env['cloud.project'].with_user(self.developer)
        self.assertTrue(mixin._check_cloud_group('group_cloud_user'))

    def test_check_cloud_group_pm_passes_for_consultant(self):
        mixin = self.env['cloud.project'].with_user(self.pm)
        self.assertTrue(mixin._check_cloud_group('group_cloud_consultant'))

    # -- _check_cloud_group: passes for superuser ----------------------

    def test_check_cloud_group_superuser_bypasses(self):
        mixin = self.env['cloud.project'].sudo()
        self.assertTrue(mixin._check_cloud_group('group_cloud_manager'))

    # -- _has_cloud_group: returns bool without raising -----------------

    def test_has_cloud_group_returns_false(self):
        mixin = self.env['cloud.project'].with_user(self.stakeholder)
        self.assertFalse(mixin._has_cloud_group('group_cloud_consultant'))

    def test_has_cloud_group_returns_true_exact(self):
        mixin = self.env['cloud.project'].with_user(self.consultant)
        self.assertTrue(mixin._has_cloud_group('group_cloud_consultant'))

    def test_has_cloud_group_returns_true_higher(self):
        mixin = self.env['cloud.project'].with_user(self.manager)
        self.assertTrue(mixin._has_cloud_group('group_cloud_user'))

    def test_has_cloud_group_superuser_always_true(self):
        mixin = self.env['cloud.project'].sudo()
        self.assertTrue(mixin._has_cloud_group('group_cloud_manager'))

    # -- _get_user_role -------------------------------------------------

    def test_get_user_role_stakeholder(self):
        mixin = self.env['cloud.project'].with_user(self.stakeholder)
        self.assertEqual(mixin._get_user_role(), 'stakeholder')

    def test_get_user_role_consultant(self):
        mixin = self.env['cloud.project'].with_user(self.consultant)
        self.assertEqual(mixin._get_user_role(), 'consultant')

    def test_get_user_role_pm(self):
        mixin = self.env['cloud.project'].with_user(self.pm)
        self.assertEqual(mixin._get_user_role(), 'project_manager')

    def test_get_user_role_developer(self):
        mixin = self.env['cloud.project'].with_user(self.developer)
        self.assertEqual(mixin._get_user_role(), 'developer')

    def test_get_user_role_manager(self):
        mixin = self.env['cloud.project'].with_user(self.manager)
        self.assertEqual(mixin._get_user_role(), 'manager')

    # -- _get_user_permissions ------------------------------------------

    def test_permissions_stakeholder(self):
        perms = self.env['cloud.project'].with_user(self.stakeholder)._get_user_permissions()
        self.assertEqual(perms['role'], 'stakeholder')
        self.assertFalse(perms['can_create_project'])
        self.assertFalse(perms['can_manage_hosts'])
        self.assertFalse(perms['can_use_terminal'])
        self.assertFalse(perms['can_deploy'])
        self.assertFalse(perms['can_create_instance'])

    def test_permissions_consultant(self):
        perms = self.env['cloud.project'].with_user(self.consultant)._get_user_permissions()
        self.assertEqual(perms['role'], 'consultant')
        self.assertTrue(perms['can_deploy'])
        self.assertTrue(perms['can_create_instance'])
        self.assertFalse(perms['can_create_project'])
        self.assertFalse(perms['can_use_terminal'])
        self.assertFalse(perms['can_manage_hosts'])

    def test_permissions_pm(self):
        perms = self.env['cloud.project'].with_user(self.pm)._get_user_permissions()
        self.assertEqual(perms['role'], 'project_manager')
        self.assertTrue(perms['can_create_project'])
        self.assertTrue(perms['can_deploy'])
        self.assertFalse(perms['can_use_terminal'])
        self.assertFalse(perms['can_manage_hosts'])

    def test_permissions_developer(self):
        perms = self.env['cloud.project'].with_user(self.developer)._get_user_permissions()
        self.assertEqual(perms['role'], 'developer')
        self.assertTrue(perms['can_use_terminal'])
        self.assertTrue(perms['can_view_logs'])
        self.assertTrue(perms['can_manage_backups'])
        self.assertTrue(perms['can_export'])
        self.assertTrue(perms['can_create_project'])
        self.assertFalse(perms['can_manage_hosts'])
        self.assertFalse(perms['can_delete_production'])

    def test_permissions_manager(self):
        perms = self.env['cloud.project'].with_user(self.manager)._get_user_permissions()
        self.assertEqual(perms['role'], 'manager')
        self.assertTrue(perms['can_manage_hosts'])
        self.assertTrue(perms['can_manage_settings'])
        self.assertTrue(perms['can_delete_production'])
        self.assertTrue(perms['can_use_terminal'])
        self.assertTrue(perms['can_create_project'])


# ── 2. Model-level protection tests ─────────────────────────────────────


class TestProjectSecurity(CloudSecurityBase):
    """Tests for cloud.project create/delete guards."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('proj_stakeholder', 'group_cloud_user')
        self.consultant = self._create_user('proj_consultant', 'group_cloud_consultant')
        self.pm = self._create_user('proj_pm', 'group_cloud_project_manager')
        self.manager = self._create_user('proj_manager', 'group_cloud_manager')

    def test_stakeholder_cannot_create_project(self):
        with self.assertRaises(AccessError):
            self.env['cloud.project'].with_user(self.stakeholder).create({
                'name': 'Forbidden Project',
            })

    def test_consultant_cannot_create_project(self):
        with self.assertRaises(AccessError):
            self.env['cloud.project'].with_user(self.consultant).create({
                'name': 'Forbidden Project',
            })

    def test_pm_can_create_project(self):
        project = self.env['cloud.project'].with_user(self.pm).create({
            'name': 'PM Project',
        })
        self.assertTrue(project.id)

    def test_manager_can_create_project(self):
        project = self.env['cloud.project'].with_user(self.manager).create({
            'name': 'Manager Project',
        })
        self.assertTrue(project.id)

    def test_developer_cannot_delete_project(self):
        project = self._create_project('To Delete')
        with self.assertRaises(AccessError):
            developer = self._create_user('proj_dev', 'group_cloud_developer')
            project.with_user(developer).unlink()

    def test_manager_can_delete_project(self):
        project = self._create_project('To Delete')
        project.with_user(self.manager).unlink()
        self.assertFalse(project.exists())


class TestInstanceSecurity(CloudSecurityBase):
    """Tests for cloud.instance create/delete guards."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('inst_stakeholder', 'group_cloud_user')
        self.consultant = self._create_user('inst_consultant', 'group_cloud_consultant')
        self.developer = self._create_user('inst_developer', 'group_cloud_developer')
        self.manager = self._create_user('inst_manager', 'group_cloud_manager')
        self.project = self._create_project(
            'Instance Project',
            members=[self.stakeholder, self.consultant, self.developer, self.manager],
        )

    def test_stakeholder_cannot_create_instance(self):
        with self.assertRaises(AccessError):
            self.env['cloud.instance'].with_user(self.stakeholder).create({
                'name': 'forbidden-inst',
                'project_id': self.project.id,
                'environment': 'staging',
            })

    def test_consultant_can_create_instance(self):
        inst = self.env['cloud.instance'].with_user(self.consultant).create({
            'name': 'ok-inst',
            'project_id': self.project.id,
            'environment': 'staging',
        })
        self.assertTrue(inst.id)

    def test_developer_cannot_delete_production_instance(self):
        inst = self._create_instance(self.project, environment='production')
        with self.assertRaises(AccessError):
            inst.with_user(self.developer).unlink()

    def test_manager_can_delete_production_instance(self):
        inst = self._create_instance(self.project, environment='production')
        inst.with_user(self.manager).unlink()
        self.assertFalse(inst.exists())

    def test_consultant_can_delete_staging_instance(self):
        inst = self._create_instance(self.project, environment='staging')
        inst.with_user(self.consultant).unlink()
        self.assertFalse(inst.exists())

    def test_stakeholder_cannot_delete_staging_instance(self):
        inst = self._create_instance(self.project, environment='staging')
        with self.assertRaises(AccessError):
            inst.with_user(self.stakeholder).unlink()


class TestHostSecurity(CloudSecurityBase):
    """Tests for cloud.host create/write/unlink guards."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('host_stakeholder', 'group_cloud_user')
        self.developer = self._create_user('host_developer', 'group_cloud_developer')
        self.manager = self._create_user('host_manager', 'group_cloud_manager')

    def _host_vals(self, name='Test Host'):
        return {
            'name': name,
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'test.example.com',
        }

    def test_stakeholder_cannot_create_host(self):
        with self.assertRaises(AccessError):
            self.env['cloud.host'].with_user(self.stakeholder).create(
                self._host_vals()
            )

    def test_developer_cannot_create_host(self):
        with self.assertRaises(AccessError):
            self.env['cloud.host'].with_user(self.developer).create(
                self._host_vals()
            )

    def test_manager_can_create_host(self):
        host = self.env['cloud.host'].with_user(self.manager).create(
            self._host_vals()
        )
        self.assertTrue(host.id)

    def test_stakeholder_cannot_write_host(self):
        host = self.env['cloud.host'].create(self._host_vals())
        with self.assertRaises(AccessError):
            host.with_user(self.stakeholder).write({'name': 'Changed'})

    def test_developer_cannot_unlink_host(self):
        host = self.env['cloud.host'].create(self._host_vals())
        with self.assertRaises(AccessError):
            host.with_user(self.developer).unlink()

    def test_manager_can_write_host(self):
        host = self.env['cloud.host'].create(self._host_vals())
        host.with_user(self.manager).write({'name': 'Updated'})
        self.assertEqual(host.name, 'Updated')

    def test_manager_can_unlink_host(self):
        host = self.env['cloud.host'].create(self._host_vals())
        host.with_user(self.manager).unlink()
        self.assertFalse(host.exists())


class TestBackupBackendSecurity(CloudSecurityBase):
    """Tests for cloud.backup.backend create/write/unlink guards."""

    def setUp(self):
        super().setUp()
        self.consultant = self._create_user('bb_consultant', 'group_cloud_consultant')
        self.developer = self._create_user('bb_developer', 'group_cloud_developer')
        self.manager = self._create_user('bb_manager', 'group_cloud_manager')

    def _backend_vals(self, name='Test Backend'):
        return {
            'name': name,
            'backend_type': 's3',
            's3_bucket': 'test-bucket',
        }

    def test_consultant_cannot_create_backend(self):
        with self.assertRaises(AccessError):
            self.env['cloud.backup.backend'].with_user(self.consultant).create(
                self._backend_vals()
            )

    def test_developer_cannot_create_backend(self):
        with self.assertRaises(AccessError):
            self.env['cloud.backup.backend'].with_user(self.developer).create(
                self._backend_vals()
            )

    def test_manager_can_create_backend(self):
        backend = self.env['cloud.backup.backend'].with_user(self.manager).create(
            self._backend_vals()
        )
        self.assertTrue(backend.id)

    def test_developer_cannot_write_backend(self):
        backend = self.env['cloud.backup.backend'].create(self._backend_vals())
        with self.assertRaises(AccessError):
            backend.with_user(self.developer).write({'name': 'Changed'})

    def test_developer_cannot_unlink_backend(self):
        backend = self.env['cloud.backup.backend'].create(self._backend_vals())
        with self.assertRaises(AccessError):
            backend.with_user(self.developer).unlink()

    def test_manager_can_write_backend(self):
        backend = self.env['cloud.backup.backend'].create(self._backend_vals())
        backend.with_user(self.manager).write({'name': 'Updated'})
        self.assertEqual(backend.name, 'Updated')

    def test_manager_can_unlink_backend(self):
        backend = self.env['cloud.backup.backend'].create(self._backend_vals())
        backend.with_user(self.manager).unlink()
        self.assertFalse(backend.exists())


# ── 3. Record rules (project visibility) ────────────────────────────────


class TestProjectRecordRules(CloudSecurityBase):
    """Tests that record rules restrict project visibility based on
    membership for Stakeholder/Consultant, while PM+ sees all."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('rr_stakeholder', 'group_cloud_user')
        self.consultant = self._create_user('rr_consultant', 'group_cloud_consultant')
        self.pm = self._create_user('rr_pm', 'group_cloud_project_manager')
        self.developer = self._create_user('rr_developer', 'group_cloud_developer')
        self.manager = self._create_user('rr_manager', 'group_cloud_manager')

        # Project A: stakeholder + consultant are members
        self.project_a = self._create_project(
            'Project A',
            members=[self.stakeholder, self.consultant],
        )
        # Project B: no stakeholder/consultant membership
        self.project_b = self._create_project('Project B')

    def test_stakeholder_sees_only_member_projects(self):
        projects = self.env['cloud.project'].with_user(self.stakeholder).search([
            ('id', 'in', (self.project_a | self.project_b).ids),
        ])
        self.assertEqual(projects, self.project_a)

    def test_consultant_sees_only_member_projects(self):
        projects = self.env['cloud.project'].with_user(self.consultant).search([
            ('id', 'in', (self.project_a | self.project_b).ids),
        ])
        self.assertEqual(projects, self.project_a)

    def test_pm_sees_all_projects(self):
        projects = self.env['cloud.project'].with_user(self.pm).search([
            ('id', 'in', (self.project_a | self.project_b).ids),
        ])
        self.assertEqual(len(projects), 2)

    def test_developer_sees_all_projects(self):
        projects = self.env['cloud.project'].with_user(self.developer).search([
            ('id', 'in', (self.project_a | self.project_b).ids),
        ])
        self.assertEqual(len(projects), 2)

    def test_manager_sees_all_projects(self):
        projects = self.env['cloud.project'].with_user(self.manager).search([
            ('id', 'in', (self.project_a | self.project_b).ids),
        ])
        self.assertEqual(len(projects), 2)


class TestInstanceRecordRules(CloudSecurityBase):
    """Tests that record rules restrict instance visibility based on
    project membership for Stakeholder/Consultant, while PM+ sees all."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('ir_stakeholder', 'group_cloud_user')
        self.consultant = self._create_user('ir_consultant', 'group_cloud_consultant')
        self.pm = self._create_user('ir_pm', 'group_cloud_project_manager')

        self.project_member = self._create_project(
            'Member Project',
            members=[self.stakeholder, self.consultant],
        )
        self.project_other = self._create_project('Other Project')

        self.inst_member = self._create_instance(self.project_member)
        self.inst_other = self._create_instance(self.project_other, name='inst-other')

    def test_stakeholder_sees_only_member_project_instances(self):
        instances = self.env['cloud.instance'].with_user(self.stakeholder).search([
            ('id', 'in', (self.inst_member | self.inst_other).ids),
        ])
        self.assertEqual(instances, self.inst_member)

    def test_consultant_sees_only_member_project_instances(self):
        instances = self.env['cloud.instance'].with_user(self.consultant).search([
            ('id', 'in', (self.inst_member | self.inst_other).ids),
        ])
        self.assertEqual(instances, self.inst_member)

    def test_pm_sees_all_instances(self):
        instances = self.env['cloud.instance'].with_user(self.pm).search([
            ('id', 'in', (self.inst_member | self.inst_other).ids),
        ])
        self.assertEqual(len(instances), 2)


# ── 4. SEC-009 — ACL lock-down, scoped rules and field-groups ───────────


@tagged('post_install', '-at_install')
class TestSec009AclLockdown(CloudSecurityBase):
    """SEC-009: models whose only read ACL was the inherited Stakeholder
    grant, with no rule to scope it, are locked down. Low roles can no
    longer read them at all; the intended higher role still can.

    post_install: the removed-ACL rows are orphan-cleaned only at the end
    of the module load (``_process_end``), after at_install tests run, so
    a mid-update at_install run would still see the stale grant.
    """

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('s9_stakeholder', 'group_cloud_user')
        self.consultant = self._create_user('s9_consultant', 'group_cloud_consultant')
        self.developer = self._create_user('s9_developer', 'group_cloud_developer')
        self.manager = self._create_user('s9_manager', 'group_cloud_manager')
        self.event = self.env['cloud.github.event'].create({
            'event_type': 'push',
            'delivery_id': 's9-delivery',
            'payload': '{"pusher": {"email": "secret@example.com"}}',
        })
        self.host = self.env['cloud.host'].create({
            'name': 'S9 Host',
            'ip_address': '10.9.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 's9.example.com',
        })
        self.whitelist = self.env['cloud.host.whitelist'].create({
            'host_id': self.host.id,
            'hostname': 'fonts.googleapis.com',
        })

    def test_github_event_stakeholder_denied(self):
        with self.assertRaises(AccessError):
            self.env['cloud.github.event'].with_user(self.stakeholder).search([])

    def test_github_event_consultant_denied(self):
        with self.assertRaises(AccessError):
            self.env['cloud.github.event'].with_user(self.consultant).search([])

    def test_github_event_manager_allowed(self):
        events = self.env['cloud.github.event'].with_user(self.manager).search([
            ('id', '=', self.event.id),
        ])
        self.assertEqual(events, self.event)

    def test_whitelist_stakeholder_denied(self):
        with self.assertRaises(AccessError):
            self.env['cloud.host.whitelist'].with_user(self.stakeholder).search([])

    def test_whitelist_consultant_denied(self):
        with self.assertRaises(AccessError):
            self.env['cloud.host.whitelist'].with_user(self.consultant).search([])

    def test_whitelist_developer_still_allowed(self):
        rows = self.env['cloud.host.whitelist'].with_user(self.developer).search([
            ('id', '=', self.whitelist.id),
        ])
        self.assertEqual(rows, self.whitelist)


class TestSec009PendingPushRules(CloudSecurityBase):
    """SEC-009: cloud.instance.pending.push keeps its ACL (the panel is
    read by project members) but is now scoped by a record rule — a
    stakeholder sees pending pushes only for their own instances, PM+ all."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('pp_stakeholder', 'group_cloud_user')
        self.pm = self._create_user('pp_pm', 'group_cloud_project_manager')
        self.project_member = self._create_project(
            'PP Member', members=[self.stakeholder],
        )
        self.project_other = self._create_project('PP Other')
        self.inst_member = self._create_instance(self.project_member)
        self.inst_other = self._create_instance(
            self.project_other, name='inst-other',
        )
        self.push_member = self.env['cloud.instance.pending.push'].create({
            'instance_id': self.inst_member.id,
            'skip_reason': 'cooldown',
            'push_by': 'member-pusher',
        })
        self.push_other = self.env['cloud.instance.pending.push'].create({
            'instance_id': self.inst_other.id,
            'skip_reason': 'cooldown',
            'push_by': 'other-pusher',
        })

    def test_stakeholder_sees_only_own_pending_push(self):
        rows = self.env['cloud.instance.pending.push'].with_user(
            self.stakeholder
        ).search([('id', 'in', (self.push_member | self.push_other).ids)])
        self.assertEqual(rows, self.push_member)

    def test_pm_sees_all_pending_pushes(self):
        rows = self.env['cloud.instance.pending.push'].with_user(
            self.pm
        ).search([('id', 'in', (self.push_member | self.push_other).ids)])
        self.assertEqual(len(rows), 2)


class TestSec009FieldGroups(CloudSecurityBase):
    """SEC-009: the SSH endpoint (ip/port/user) is field-gated to Developer
    at the ORM layer — the same gate as the host credential, and the role
    that legitimately opens SSH jobs — while ``allowed_ssh_ips`` and the
    backup access key are Administrator-only. A lower role that can still
    read the record (name, for the picker/instance form) cannot reach the
    sensitive columns even by raw RPC."""

    def setUp(self):
        super().setUp()
        self.stakeholder = self._create_user('fg_stakeholder', 'group_cloud_user')
        self.consultant = self._create_user('fg_consultant', 'group_cloud_consultant')
        self.developer = self._create_user('fg_developer', 'group_cloud_developer')
        self.manager = self._create_user('fg_manager', 'group_cloud_manager')
        self.host = self.env['cloud.host'].create({
            'name': 'FG Host',
            'ip_address': '10.9.0.2',
            'user': 'ubuntu',
            'port': 2222,
            'allowed_ssh_ips': '203.0.113.4',
            'wildcard_domain': 'fg.example.com',
        })
        self.backend = self.env['cloud.backup.backend'].create({
            'name': 'FG Backend',
            'backend_type': 's3',
            's3_bucket': 'fg-bucket',
            's3_access_key_id': 'AKIAFAKE',
        })

    def test_stakeholder_can_read_host_name(self):
        # The picker still needs id/name — only the SSH endpoint is gated.
        rows = self.env['cloud.host'].with_user(self.stakeholder).search_read(
            [('id', '=', self.host.id)], ['name'],
        )
        self.assertEqual(rows[0]['name'], 'FG Host')

    def test_stakeholder_cannot_read_host_ip(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.stakeholder).read(['ip_address'])

    def test_stakeholder_cannot_read_host_user_port(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.stakeholder).read(['user', 'port'])

    def test_consultant_cannot_read_host_ssh_endpoint(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).read(
                ['ip_address', 'user', 'port']
            )

    def test_developer_can_read_host_ssh_endpoint(self):
        # Same gate as password/key_file: a Developer opens SSH jobs that
        # run under their own user, so the endpoint must stay readable.
        data = self.host.with_user(self.developer).read(
            ['ip_address', 'user', 'port']
        )[0]
        self.assertEqual(data['ip_address'], '10.9.0.2')
        self.assertEqual(data['port'], 2222)

    def test_developer_reads_the_executor_connection_triplet(self):
        # Jobs run under the enqueuing user's env and the executor reads
        # ip/port/user by attribute (``abstract_executor.__init__``). A
        # Developer-triggered job must not AccessError there — it did when
        # these fields were manager-gated.
        h = self.host.with_user(self.developer)
        self.assertEqual(
            (h.ip_address, h.port, h.user), ('10.9.0.2', 2222, 'ubuntu'),
        )

    def test_stakeholder_cannot_read_allowed_ssh_ips(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.stakeholder).read(['allowed_ssh_ips'])

    def test_developer_cannot_read_allowed_ssh_ips(self):
        # Hardening configuration stays Administrator-only.
        with self.assertRaises(AccessError):
            self.host.with_user(self.developer).read(['allowed_ssh_ips'])

    def test_manager_can_read_host_ssh_endpoint(self):
        data = self.host.with_user(self.manager).read(
            ['ip_address', 'user', 'port', 'allowed_ssh_ips']
        )[0]
        self.assertEqual(data['ip_address'], '10.9.0.2')
        self.assertEqual(data['port'], 2222)
        self.assertEqual(data['allowed_ssh_ips'], '203.0.113.4')

    def test_stakeholder_cannot_read_backend_access_key(self):
        with self.assertRaises(AccessError):
            self.backend.with_user(self.stakeholder).read(['s3_access_key_id'])

    def test_manager_can_read_backend_access_key(self):
        data = self.backend.with_user(self.manager).read(['s3_access_key_id'])[0]
        self.assertEqual(data['s3_access_key_id'], 'AKIAFAKE')


class TestSec009ControllerRedaction(CloudSecurityBase):
    """SEC-009 at the SPA boundary: the instance serializers redact the
    host SSH endpoint below Developer and the bucket path below
    Administrator, without raising for the lower role — and the backup
    backend list is manager-gated. Exercised through the real controller
    methods with ``request`` bound to the role's env, the way the JSON-RPC
    layer would."""

    def setUp(self):
        super().setUp()
        from types import SimpleNamespace
        from unittest.mock import patch
        from odoo.addons.incubacloud.controllers.data_load import (
            CloudDataLoadController,
        )
        self._SimpleNamespace = SimpleNamespace
        self._patch = patch
        self.ctrl = CloudDataLoadController()
        self.consultant = self._create_user('cr_consultant', 'group_cloud_consultant')
        self.developer = self._create_user('cr_developer', 'group_cloud_developer')
        self.manager = self._create_user('cr_manager', 'group_cloud_manager')
        self.host = self.env['cloud.host'].create({
            'name': 'CR Host',
            'ip_address': '10.9.0.3',
            'user': 'deploy',
            'port': 2200,
            'wildcard_domain': 'cr.example.com',
        })
        self.backend = self.env['cloud.backup.backend'].create({
            'name': 'CR Backend',
            'backend_type': 's3',
            's3_bucket': 'cr-bucket',
        })
        self.project = self._create_project(
            'CR Project', members=[self.consultant, self.developer],
        )
        self.inst = self._create_instance(
            self.project, host_id=self.host.id,
            backup_backend_id=self.backend.id,
        )

    def _bound(self, user):
        """Bind ``request`` to *user*'s env in every module the route touches."""
        fake = self._SimpleNamespace(env=self.env(user=user))
        base = 'odoo.addons.incubacloud.controllers.'
        return (
            self._patch(base + '_data_load._routes_crud.request', fake),
            self._patch(base + '_data_load._routes_backends.request', fake),
            self._patch(base + 'data_load.request', fake),
            self._patch('odoo.http.request', fake),
        )

    def _with(self, user):
        from contextlib import ExitStack
        stack = ExitStack()
        for p in self._bound(user):
            stack.enter_context(p)
        return stack

    # -- instance serializer ---------------------------------------------

    def test_consultant_gets_host_name_but_no_ssh_endpoint(self):
        with self._with(self.consultant):
            data = self.ctrl._serialize_instance(
                self.inst.with_user(self.consultant)
            )
        self.assertEqual(data['host'], 'CR Host')
        self.assertEqual(data['host_ip'], '')
        self.assertEqual(data['host_user'], '')
        self.assertEqual(data['host_port'], 22)
        self.assertEqual(data['effective_backup_backend_name'], 'CR Backend')

    def test_developer_gets_the_real_ssh_endpoint(self):
        # The restore dialog builds its rsync command from these.
        with self._with(self.developer):
            data = self.ctrl._serialize_instance(
                self.inst.with_user(self.developer)
            )
        self.assertEqual(data['host_ip'], '10.9.0.3')
        self.assertEqual(data['host_user'], 'deploy')
        self.assertEqual(data['host_port'], 2200)

    def test_project_instances_redact_for_consultant(self):
        with self._with(self.consultant):
            data = self.ctrl.cloud_get_project_instances(self.project.id)
        row = next(r for r in data['instances'] if r['id'] == self.inst.id)
        self.assertEqual(row['host'], 'CR Host')
        self.assertEqual((row['host_ip'], row['host_user'], row['host_port']),
                         ('', '', 22))

    def test_project_full_redacts_endpoint_and_bucket_below_manager(self):
        with self._with(self.developer):
            data = self.ctrl.cloud_get_project_full(self.project.id)
        # Developer: endpoint yes, bucket path no (manager-only).
        self.assertEqual(data['instances'][self.inst.id]['host_ip'], '10.9.0.3')
        bb = next(b for b in data['backup_backends'] if b['id'] == self.backend.id)
        self.assertEqual(bb['name'], 'CR Backend')
        self.assertEqual(bb['backup_dst'], '')

    def test_project_full_gives_manager_the_bucket_path(self):
        with self._with(self.manager):
            data = self.ctrl.cloud_get_project_full(self.project.id)
        bb = next(b for b in data['backup_backends'] if b['id'] == self.backend.id)
        self.assertIn('cr-bucket', bb['backup_dst'])

    # -- backup backend list -----------------------------------------------

    def test_backup_backends_list_denied_below_manager(self):
        for user in (self.consultant, self.developer):
            with self.subTest(user=user.login), self._with(user), \
                    self.assertRaises(AccessError):
                self.ctrl.cloud_get_backup_backends()

    def test_backup_backends_list_allowed_for_manager(self):
        with self._with(self.manager):
            data = self.ctrl.cloud_get_backup_backends()
        self.assertIn(self.backend.id, [b['id'] for b in data['items']])
