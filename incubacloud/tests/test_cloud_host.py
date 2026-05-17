"""
Tier 2 — ORM integration tests for cloud.host.
Verifies Traefik template defaults, required fields, and the
``_release_external_resources`` lifecycle hook fired before a host
is archived or unlinked.
"""
from unittest.mock import MagicMock, patch

from odoo import http
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user


class TestCloudHostTraefikDefaults(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Host = self.env['cloud.host']

    def _create(self, **kw):
        return self.Host.create({
            'name': 'Test Host',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'example.com',
        } | kw)

    def test_create_applies_defaults(self):
        """Host without traefik fields gets templates."""
        host = self._create()
        self.assertTrue(host.traefik_config_yml)
        self.assertTrue(host.traefik_inverseproxy_yaml)
        self.assertTrue(host.traefik_yml)

    def test_create_preserves_explicit(self):
        """Explicit traefik fields are not overwritten."""
        custom = "# custom config"
        host = self._create(traefik_config_yml=custom)
        self.assertEqual(host.traefik_config_yml, custom)

    def test_create_empty_string_gets_defaults(self):
        """Empty strings from frontend get replaced."""
        host = self._create(
            traefik_config_yml='',
            traefik_inverseproxy_yaml='',
            traefik_yml='',
        )
        self.assertTrue(host.traefik_config_yml)
        self.assertTrue(host.traefik_inverseproxy_yaml)
        self.assertTrue(host.traefik_yml)

    def test_defaults_contain_traefik(self):
        """Templates contain recognizable Traefik config."""
        host = self._create()
        self.assertIn('traefik', host.traefik_yml.lower())
        self.assertIn(
            'traefik',
            host.traefik_inverseproxy_yaml.lower(),
        )


class TestCloudHostReleaseHook(TransactionCase):
    """The ``_release_external_resources`` hook must fire on every
    transition that disables or removes a host so derived modules
    can release attached external resources without having to
    override write/unlink themselves."""

    def setUp(self):
        super().setUp()
        self.Host = self.env['cloud.host']
        self.host = self.Host.create({
            'name': 'Hook Target',
            'ip_address': '10.0.0.2',
            'user': 'ubuntu',
            'wildcard_domain': 'example.com',
        })

    def test_hook_is_noop_by_default(self):
        """Default implementation must be a no-op so installs without
        any extension keep working unchanged."""
        result = self.host._release_external_resources()
        self.assertIsNone(result)

    def test_write_active_false_calls_hook(self):
        """Archiving an active host triggers the hook before super()."""
        with patch.object(
            type(self.host), '_release_external_resources',
            autospec=True,
        ) as hook:
            self.host.write({'active': False})
        hook.assert_called_once()
        called_with = hook.call_args.args[0]
        self.assertEqual(called_with, self.host)

    def test_write_active_true_does_not_call_hook(self):
        """Re-activating a host must not fire the release hook."""
        self.host.write({'active': False})
        with patch.object(
            type(self.host), '_release_external_resources',
            autospec=True,
        ) as hook:
            self.host.write({'active': True})
        hook.assert_not_called()

    def test_write_other_field_does_not_call_hook(self):
        """Writing unrelated fields keeps the hook untouched."""
        with patch.object(
            type(self.host), '_release_external_resources',
            autospec=True,
        ) as hook:
            self.host.write({'name': 'Renamed'})
        hook.assert_not_called()

    def test_write_active_false_skips_already_inactive(self):
        """Already-inactive hosts inside the recordset are excluded
        from the hook call — only the transitioning ones get released."""
        inactive = self.Host.create({
            'name': 'Already off',
            'ip_address': '10.0.0.3',
            'user': 'ubuntu',
            'wildcard_domain': 'example.com',
            'active': False,
        })
        recordset = self.host | inactive
        with patch.object(
            type(self.host), '_release_external_resources',
            autospec=True,
        ) as hook:
            recordset.write({'active': False})
        hook.assert_called_once()
        called_with = hook.call_args.args[0]
        self.assertEqual(called_with, self.host)
        self.assertNotIn(inactive, called_with)

    def test_unlink_calls_hook(self):
        """Unlinking a host fires the hook before super().unlink()."""
        with patch.object(
            type(self.host), '_release_external_resources',
            autospec=True,
        ) as hook:
            self.host.unlink()
        hook.assert_called_once()


# Note: the ``/cloud/delete_host`` route is a one-line branch on top of
# the model-level ``write/unlink`` covered above — calling it from a
# bare TransactionCase requires faking the full ``odoo.http.request``
# proxy, which is more plumbing than the branch is worth. The
# end-to-end behaviour is checked manually per the plan's Verificación
# table; if that ever changes, an ``HttpCase`` test with authentication
# is the right tool.


class TestCloudHostRBACGates(TransactionCase):
    """Security regression suite: the lifecycle hook reorganisation
    must not weaken any existing manager-role gate on
    ``cloud.host.write``, ``cloud.host.unlink`` or the
    ``/cloud/delete_host`` route. A non-manager who lands a stray
    call on any of those paths must still get an ``AccessError``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Some optional addons add NOT NULL columns to res_partner
        # without server defaults — ``new_test_user`` then trips them
        # at partner-create. Mirror the workaround used by the wider
        # security suite so this file stays self-contained.
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

    def setUp(self):
        super().setUp()
        self.consultant = new_test_user(
            self.env, login='host_rbac_consultant',
            groups='base.group_user,incubacloud.group_cloud_consultant',
        )
        self.host = self.env['cloud.host'].create({
            'name': 'Gate Target',
            'ip_address': '10.0.0.60',
            'user': 'ubuntu',
            'wildcard_domain': 'example.com',
        })

    def test_non_manager_cannot_archive(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).write({'active': False})
        self.assertTrue(self.host.exists())
        self.assertTrue(self.host.active)

    def test_non_manager_cannot_unlink(self):
        with self.assertRaises(AccessError):
            self.host.with_user(self.consultant).unlink()
        self.assertTrue(self.host.exists())

    def test_archive_writes_audit_entry(self):
        """``active`` is in ``_AUDIT_TRACKED_FIELDS`` — archive
        transitions must keep generating a Config-changed audit entry
        after the hook reorganisation."""
        before = self.env['cloud.audit.log'].search_count([
            ('host_id', '=', self.host.id),
            ('action', '=', 'Config changed'),
        ])
        self.host.write({'active': False})
        after = self.env['cloud.audit.log'].search_count([
            ('host_id', '=', self.host.id),
            ('action', '=', 'Config changed'),
        ])
        self.assertEqual(after - before, 1)

    def test_unlink_writes_audit_entry(self):
        """``unlink`` leaves a ``Host deleted`` row in the audit log
        (``host_id`` is ``ondelete='set null'``, so the entry survives
        the host going away)."""
        host_name = self.host.name
        before = self.env['cloud.audit.log'].search_count([
            ('action', '=', 'Host deleted'),
            ('details', '=', host_name),
        ])
        self.host.unlink()
        after = self.env['cloud.audit.log'].search_count([
            ('action', '=', 'Host deleted'),
            ('details', '=', host_name),
        ])
        self.assertEqual(after - before, 1)


class TestCloudDeleteHostRoute(TransactionCase):
    """``/cloud/delete_host`` no-traefik branch must choose between
    ``unlink`` (truly empty host) and ``archive`` (host with job
    history) so the ``cloud.job.host_id`` FK doesn't trip when a host
    accumulates a job before Traefik is deployed.

    Concrete trigger that motivated the branch: on-demand VPS chain
    (``tenant_vps_provision → host_hardening → full_setup``) — the
    provision job succeeds and lands on ``cloud.job`` but the chain
    breaks before Traefik is deployed, so ``host.traefik_deployed`` is
    still False while ``cloud_job_count >= 1``. The old ``unlink``
    branch then violated the FK and the user got the Odoo "Another
    model is using the record …" pop-up.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Same NOT NULL workaround the wider security suite uses so the
        # manager test user can be created without tripping on optional
        # res_partner columns added by side modules.
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

    def setUp(self):
        super().setUp()
        self.manager = new_test_user(
            self.env, login='host_delete_manager',
            groups='base.group_user,incubacloud.group_cloud_manager',
        )
        self.host = self.env['cloud.host'].create({
            'name': 'Delete Target',
            'ip_address': '10.0.0.99',
            'user': 'ubuntu',
            'wildcard_domain': 'delete.example.com',
        })
        # Ensure the precondition the no-traefik branch reacts to.
        # ``traefik_deployed`` defaults to False but we make the
        # assumption explicit so a future model change can't silently
        # invalidate the tests.
        self.assertFalse(self.host.traefik_deployed)

    def _invoke_delete(self, host_id):
        """Drive ``cloud_delete_host`` against the test cursor.

        The route reads ``odoo.http.request`` for env + httprequest; we
        patch the module-level proxy with a ``MagicMock`` spec'd
        against the real ``http.Request`` class so any access outside
        the API surface fails loudly (per project policy on mocks).
        """
        from odoo.addons.incubacloud.controllers import data_load
        from odoo.addons.incubacloud.controllers._data_load import (
            _routes_crud,
        )
        fake_request = MagicMock(spec=http.Request)
        # Spec'd against the real class so any access outside the
        # documented API surface fails loudly. The route plus the
        # RBAC helper only touch ``request.env``; if a future change
        # starts reading another attribute, this mock raises
        # ``AttributeError`` and the test points the way.
        fake_request.env = self.env(user=self.manager)
        # CrudMixin's ``self._sec()`` and ``request.env`` lookups
        # resolve via ``odoo.http.request``, the werkzeug LocalProxy.
        # Patch the proxy globally so the resolution through
        # ``data_load.request`` and ``_routes_crud.request`` (each its
        # own module-local import) both land on the fake.
        ctrl = data_load.CloudDataLoadController()
        with patch.object(http, 'request', fake_request), \
                patch.object(data_load, 'request', fake_request), \
                patch.object(_routes_crud, 'request', fake_request):
            return ctrl.cloud_delete_host(host_id)

    def test_empty_host_no_traefik_is_unlinked(self):
        """Truly-empty host (no jobs, no Traefik) takes the unlink
        fast path — same behaviour as before the F-branch fix."""
        host_id = self.host.id
        result = self._invoke_delete(host_id)
        self.assertEqual(result, {'ok': True})
        self.assertFalse(
            self.env['cloud.host'].browse(host_id).exists(),
            "Empty host must be unlinked, not archived.",
        )

    def test_host_with_jobs_is_archived_and_preserves_jobs(self):
        """Host carrying any ``cloud.job`` row must be archived (not
        unlinked) so the FK stays valid and the job history survives.

        This is the regression for the on-demand VPS path where
        ``tenant_vps_provision`` succeeded but ``host_hardening`` +
        ``full_setup`` never ran.
        """
        job_type = self.env['cloud.job.type'].sudo().search([], limit=1)
        self.assertTrue(
            job_type,
            "At least one cloud.job.type must be seeded by the module.",
        )
        job = self.env['cloud.job'].sudo().create({
            'name': 'historic provision',
            'host_id': self.host.id,
            'job_type_id': job_type.id,
        })

        host_id = self.host.id
        result = self._invoke_delete(host_id)
        self.assertEqual(result, {'ok': True})

        # Host survived the call …
        survivor = self.env['cloud.host'].browse(host_id)
        self.assertTrue(survivor.exists())
        # … and is archived (active=False), matching the deployed
        # branch's semantics so the SPA hides it from the host list.
        self.assertFalse(survivor.active)
        # Job history is preserved verbatim — the whole point of the
        # archive path over the FK-violating unlink path.
        self.assertTrue(job.exists())
        self.assertEqual(job.host_id, survivor)
