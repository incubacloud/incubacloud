"""Tests for the connect-as hardening (Imprescindible #4).

Connect-as used to be a flat Stakeholder+ action: any cloud role could
impersonate any user of any instance, production and tenant admins
included, with no throttle. The agreed shape gates it by environment:

              | Staging      | Production
    normal    | Stakeholder+ | Developer+
    tenant admin | Stakeholder+ | Manager

Both routes are additionally rate-limited per panel user and per
instance, and the token carries the panel login so the tenant can log
who opened the session.

``run_async`` is patched throughout so no SSH connection is ever
attempted; the point of most assertions is precisely that the guard
short-circuits *before* reaching the remote host.
"""
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.http import Request

from odoo.addons.incubacloud.controllers import _rate_limit as rate_limit_mod
from odoo.addons.incubacloud.controllers import connect as connect_ctl

from .test_cloud_security import CloudSecurityBase


class ConnectAsBase(CloudSecurityBase):
    """Shared fixtures: one project, one host, one instance per env."""

    def setUp(self):
        super().setUp()
        self.project = self._create_project(name='connect-proj')
        self.host = self.env['cloud.host'].create({
            'name': 'connect-host',
            'ip_address': '10.0.0.20',
            'port': 22,
            'user': 'root',
            'login_type': 'ssh_key',
            'wildcard_domain': 'connect.example.com',
        })

    def _request_for(self, user, **ctx):
        """Return a spec'd request mock bound to *user*'s environment."""
        req = MagicMock(spec=Request)
        req.env = self.env(user=user, context=self.env.context | ctx)
        return req

    @contextmanager
    def _as(self, user, **ctx):
        """Run the block with the controller's request bound to *user*.

        Both the module-level alias and ``odoo.http.request`` are
        patched: the endpoints read the alias, while ``_()`` and
        ``http.Controller.env`` resolve the global one.
        """
        req = self._request_for(user, **ctx)
        with ExitStack() as stack:
            stack.enter_context(patch.object(connect_ctl, 'request', req))
            # The shared rate-limit gate reads its own module alias.
            stack.enter_context(patch.object(rate_limit_mod, 'request', req))
            stack.enter_context(patch('odoo.http.request', req))
            yield req

    def _instance(self, environment):
        return self._create_instance(
            self.project, environment=environment,
            host_id=self.host.id,
            state='deployed',
            running=True,
            domain=f'{environment}.connect.example.com',
        )


# ── 1. The gate itself ──────────────────────────────────────────────────


class TestConnectAsGate(ConnectAsBase):
    """The matrix agreed in the decision log, checked on the mixin."""

    def setUp(self):
        super().setUp()
        self.staging = self._instance('staging')
        self.production = self._instance('production')

    def _check(self, user, instance, target_is_admin):
        """Run the gate as *user*; return True when it allows."""
        mixin = self.env['cloud.security.mixin'].with_user(user)
        try:
            mixin._check_can_connect_as_user(
                instance=instance, target_is_admin=target_is_admin,
            )
        except AccessError:
            return False
        return True

    def test_staging_allows_stakeholder_for_normal_user(self):
        user = self._create_user('ca_stake_a', 'group_cloud_user')
        self.assertTrue(self._check(user, self.staging, False))

    def test_staging_allows_stakeholder_for_tenant_admin(self):
        # The owner's own use case: a client watching their staging.
        user = self._create_user('ca_stake_b', 'group_cloud_user')
        self.assertTrue(self._check(user, self.staging, True))

    def test_production_denies_stakeholder_for_normal_user(self):
        user = self._create_user('ca_stake_c', 'group_cloud_user')
        self.assertFalse(self._check(user, self.production, False))

    def test_production_denies_stakeholder_for_tenant_admin(self):
        user = self._create_user('ca_stake_d', 'group_cloud_user')
        self.assertFalse(self._check(user, self.production, True))

    def test_production_allows_developer_for_normal_user(self):
        user = self._create_user('ca_dev_a', 'group_cloud_developer')
        self.assertTrue(self._check(user, self.production, False))

    def test_production_denies_developer_for_tenant_admin(self):
        # The one combination that needs the top role.
        user = self._create_user('ca_dev_b', 'group_cloud_developer')
        self.assertFalse(self._check(user, self.production, True))

    def test_production_allows_manager_for_normal_user(self):
        user = self._create_user('ca_mgr_a', 'group_cloud_manager')
        self.assertTrue(self._check(user, self.production, False))

    def test_production_allows_manager_for_tenant_admin(self):
        user = self._create_user('ca_mgr_b', 'group_cloud_manager')
        self.assertTrue(self._check(user, self.production, True))

    def test_no_instance_keeps_the_legacy_stakeholder_floor(self):
        """Callers that do not resolve an instance are unaffected."""
        user = self._create_user('ca_stake_e', 'group_cloud_user')
        mixin = self.env['cloud.security.mixin'].with_user(user)
        mixin._check_can_connect_as_user()  # must not raise

    def test_permissions_payload_exposes_the_three_flags(self):
        dev = self._create_user('ca_dev_c', 'group_cloud_developer')
        perms = self.env['cloud.security.mixin'].with_user(
            dev,
        )._get_user_permissions()
        self.assertTrue(perms['can_connect_as'])
        self.assertTrue(perms['can_connect_production'])
        self.assertFalse(perms['can_connect_production_admin'])


# ── 2. The routes ───────────────────────────────────────────────────────


class TestConnectAsRoutes(ConnectAsBase):
    """The controller must apply the gate before touching the host."""

    def setUp(self):
        super().setUp()
        self.staging = self._instance('staging')
        self.production = self._instance('production')
        self.controller = connect_ctl.InstanceConnectController()

    def _member(self, login, group):
        """Create a user in *group* who can see the fixture project."""
        user = self._create_user(login, group)
        self.project.member_ids = [(4, user.id)]
        return user

    def test_get_users_denied_on_production_for_stakeholder(self):
        user = self._member('cr_stake_a', 'group_cloud_user')
        with self._as(user), \
             patch.object(connect_ctl, 'run_async', autospec=True) as run:
            with self.assertRaises(AccessError):
                self.controller.get_instance_users(self.production.id)
        run.assert_not_called()

    def test_get_users_allowed_on_staging_for_stakeholder(self):
        user = self._member('cr_stake_b', 'group_cloud_user')
        with self._as(user), \
             patch.object(
                 connect_ctl, 'run_async', autospec=True,
                 return_value={'ok': True, 'users': []},
             ) as run:
            result = self.controller.get_instance_users(self.staging.id)
        self.assertTrue(result['ok'])
        run.assert_called_once()

    def test_prepare_connect_denied_on_production_for_stakeholder(self):
        user = self._member('cr_stake_c', 'group_cloud_user')
        with self._as(user), \
             patch.object(connect_ctl, 'run_async', autospec=True) as run:
            with self.assertRaises(AccessError):
                self.controller.prepare_instance_connect(
                    self.production.id, 2,
                )
        run.assert_not_called()

    def test_prepare_connect_on_production_forbids_admin_via_script(self):
        """A Developer reaches the tenant, but with allow_admin False.

        The refusal is decided inside the tenant database — the panel
        never trusts a client-supplied "this user is not an admin".
        """
        user = self._member('cr_dev_a', 'group_cloud_developer')
        script = MagicMock(spec=str)
        script.format.return_value = 'pass'
        with self._as(user), \
             patch.object(connect_ctl, '_INJECT_SESSION_SCRIPT', script), \
             patch.object(
                 connect_ctl, 'run_async', autospec=True,
                 return_value={'ok': False, 'is_admin': True,
                               'error': 'nope'},
             ):
            result = self.controller.prepare_instance_connect(
                self.production.id, 2,
            )
        self.assertFalse(result['ok'])
        self.assertFalse(script.format.call_args.kwargs['allow_admin'])

    def test_prepare_connect_on_production_allows_admin_for_manager(self):
        user = self._member('cr_mgr_b', 'group_cloud_manager')
        script = MagicMock(spec=str)
        script.format.return_value = 'pass'
        with self._as(user), \
             patch.object(connect_ctl, '_INJECT_SESSION_SCRIPT', script), \
             patch.object(
                 connect_ctl, 'run_async', autospec=True,
                 return_value={'ok': True, 'token': 'a' * 32},
             ):
            result = self.controller.prepare_instance_connect(
                self.production.id, 2, user_name='Mitchell',
            )
        self.assertTrue(result['ok'])
        kwargs = script.format.call_args.kwargs
        self.assertTrue(kwargs['allow_admin'])
        self.assertEqual(kwargs['by'], user.login)

    def test_prepare_connect_invalid_user_id_is_rejected(self):
        user = self._member('cr_mgr_a', 'group_cloud_manager')
        with self._as(user), \
             patch.object(connect_ctl, 'run_async', autospec=True) as run:
            result = self.controller.prepare_instance_connect(
                self.staging.id, 0,
            )
        self.assertFalse(result['ok'])
        run.assert_not_called()

    def test_connect_as_is_audited_with_the_environment(self):
        user = self._member('cr_mgr_c', 'group_cloud_manager')
        with self._as(user), \
             patch.object(
                 connect_ctl, 'run_async', autospec=True,
                 return_value={'ok': True, 'token': 'b' * 32},
             ):
            self.controller.prepare_instance_connect(
                self.production.id, 5, user_name='Mitchell',
            )
        log = self.env['cloud.audit.log'].sudo().search([
            ('action', '=', 'Connect as user'),
            ('instance_id', '=', self.production.id),
        ], limit=1)
        self.assertTrue(log)
        self.assertEqual(log.details, 'Mitchell [production]')


# ── 3. Rate limiting ────────────────────────────────────────────────────


class TestConnectAsRateLimit(ConnectAsBase):
    """Both caps must short-circuit before any SSH work."""

    def setUp(self):
        super().setUp()
        self.staging = self._instance('staging')
        self.controller = connect_ctl.InstanceConnectController()
        self.user = self._create_user('cl_mgr_a', 'group_cloud_manager')
        self.project.member_ids = [(4, self.user.id)]
        settings = self.env['cloud.settings'].sudo()._get()
        settings.write({
            'rate_limit_connect_user_per_min': 2,
            'rate_limit_connect_per_min': 50,
        })

    def test_user_cap_blocks_the_call_over_the_window(self):
        with self._as(self.user, force_rate_limit=True), \
             patch.object(
                 connect_ctl, 'run_async', autospec=True,
                 return_value={'ok': True, 'users': []},
             ) as run:
            for _ in range(2):
                self.assertTrue(
                    self.controller.get_instance_users(self.staging.id)['ok'],
                )
            blocked = self.controller.get_instance_users(self.staging.id)
        self.assertFalse(blocked['ok'])
        self.assertIn('Too many', blocked['error'])
        # The throttled call never reached the host.
        self.assertEqual(run.call_count, 2)

    def test_instance_cap_blocks_across_users(self):
        other = self._create_user('cl_mgr_b', 'group_cloud_manager')
        self.project.member_ids = [(4, other.id)]
        settings = self.env['cloud.settings'].sudo()._get()
        settings.write({
            'rate_limit_connect_user_per_min': 50,
            'rate_limit_connect_per_min': 1,
        })
        with patch.object(
            connect_ctl, 'run_async', autospec=True,
            return_value={'ok': True, 'users': []},
        ):
            with self._as(self.user, force_rate_limit=True):
                self.assertTrue(
                    self.controller.get_instance_users(self.staging.id)['ok'],
                )
            with self._as(other, force_rate_limit=True):
                blocked = self.controller.get_instance_users(self.staging.id)
        self.assertFalse(blocked['ok'])
        self.assertIn('this instance', blocked['error'])


# ── 4. The scripts sent to the tenant ───────────────────────────────────


class TestConnectAsScripts(ConnectAsBase):
    """The remote scripts must carry the admin detection and the marker."""

    def test_get_users_script_selects_the_admin_flag(self):
        script = connect_ctl._GET_USERS_SCRIPT.format(
            db='prod', user='odoo', password='x',
            is_admin_sql=connect_ctl._IS_ADMIN_SQL,
        )
        self.assertIn('res_groups_users_rel', script)
        self.assertIn('group_system', script)
        self.assertIn("'is_admin': bool(r[3])", script)
        # The SQL is interpolated inside a double-quoted literal: a
        # newline there would break the generated script.
        self.assertNotIn('\n', connect_ctl._IS_ADMIN_SQL)

    def test_inject_script_refuses_admin_when_not_allowed(self):
        script = connect_ctl._INJECT_SESSION_SCRIPT.format(
            db='prod', user='odoo', password='x', uid=7,
            is_admin_sql=connect_ctl._IS_ADMIN_SQL,
            allow_admin=False,
            by='operator@example.com',
        )
        self.assertIn('elif row[1] and not False:', script)
        self.assertIn('requires the Manager role', script)

    def test_inject_script_carries_the_panel_login_marker(self):
        script = connect_ctl._INJECT_SESSION_SCRIPT.format(
            db='prod', user='odoo', password='x', uid=7,
            is_admin_sql=connect_ctl._IS_ADMIN_SQL,
            allow_admin=True,
            by='operator@example.com',
        )
        self.assertIn("'by': 'operator@example.com'", script)

    def test_scripts_are_valid_python(self):
        """Formatting must not produce a syntactically broken script."""
        for script in (
            connect_ctl._GET_USERS_SCRIPT.format(
                db='prod', user='odoo', password='x',
                is_admin_sql=connect_ctl._IS_ADMIN_SQL,
            ),
            connect_ctl._INJECT_SESSION_SCRIPT.format(
                db='prod', user='odoo', password='x', uid=7,
                is_admin_sql=connect_ctl._IS_ADMIN_SQL,
                allow_admin=True, by='op',
            ),
        ):
            compile(script, '<remote>', 'exec')
