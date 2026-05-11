"""
Tier 2 — ORM integration tests for cloud.host.
Verifies Traefik template defaults, required fields, and the
``_release_external_resources`` lifecycle hook fired before a host
is archived or unlinked.
"""
from unittest.mock import patch

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
