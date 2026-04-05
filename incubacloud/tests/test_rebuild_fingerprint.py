"""
TDD — Smart Rebuild Fingerprint tests.

These tests validate the rebuild_fingerprint mechanism that determines
whether a full rebuild (--pull --no-cache) or a cached rebuild is needed.
"""
from odoo.tests.common import TransactionCase


class TestRebuildFingerprint(TransactionCase):
    """Test fingerprint computation for smart rebuild."""

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({
            'name': 'FP Test',
        })

    def _inst(self, **kw):
        base = {
            'name': 'fp-test',
            'project_id': self.project.id,
            'environment': 'staging',
        }
        return self.env['cloud.instance'].create(base | kw)

    # ── Fingerprint existence and determinism ──

    def test_fingerprint_not_empty(self):
        """A new instance should have a non-empty fingerprint."""
        inst = self._inst()
        self.assertTrue(inst.rebuild_fingerprint)

    def test_fingerprint_is_string(self):
        """Fingerprint should be a hex string."""
        inst = self._inst()
        self.assertIsInstance(inst.rebuild_fingerprint, str)
        self.assertGreater(len(inst.rebuild_fingerprint), 8)

    def test_fingerprint_deterministic(self):
        """Same data should produce same fingerprint after invalidation."""
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.invalidate_recordset()
        self.assertEqual(inst.rebuild_fingerprint, fp1)

    # ── Fields that SHOULD change the fingerprint ──

    def test_fingerprint_changes_on_pip(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.write({'pip_dependencies': 'requests==2.31'})
        self.assertNotEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_changes_on_apt(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.write({'apt_dependencies': 'libxml2-dev'})
        self.assertNotEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_changes_on_odoo_version(self):
        inst = self._inst(odoo_version='19.0')
        fp1 = inst.rebuild_fingerprint
        inst.write({'odoo_version': '18.0'})
        self.assertNotEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_changes_on_odoo_commit_sha(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.write({'odoo_commit_sha': 'abc123def456'})
        self.assertNotEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_changes_on_odoo_proxy(self):
        inst = self._inst(odoo_proxy='traefik')
        fp1 = inst.rebuild_fingerprint
        inst.write({'odoo_proxy': 'none'})
        self.assertNotEqual(inst.rebuild_fingerprint, fp1)

    # ── Fields that should NOT change the fingerprint ──

    def test_fingerprint_stable_on_domain_change(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        self.env['cloud.instance.domain'].create({
            'instance_id': inst.id,
            'hostname': 'test.example.com',
        })
        inst.invalidate_recordset()
        self.assertEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_stable_on_smtp_change(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.write({'smtp_relay_host': 'mail.example.com'})
        self.assertEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_stable_on_conf_change(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.write({'odoo_conf': '[options]\nworkers=4'})
        self.assertEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_stable_on_resource_change(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.write({'odoo_memory_limit': '4g', 'odoo_cpus': 4.0})
        self.assertEqual(inst.rebuild_fingerprint, fp1)

    def test_fingerprint_stable_on_postgres_change(self):
        inst = self._inst()
        fp1 = inst.rebuild_fingerprint
        inst.write({'postgres_dbname': 'newdb'})
        self.assertEqual(inst.rebuild_fingerprint, fp1)

    # ── Last rebuild fingerprint ──

    def test_last_rebuild_fingerprint_initially_empty(self):
        """New instances have no last_rebuild_fingerprint."""
        inst = self._inst()
        self.assertFalse(inst.last_rebuild_fingerprint)

    def test_last_rebuild_fingerprint_writable(self):
        """Can set last_rebuild_fingerprint (done by executor on success)."""
        inst = self._inst()
        inst.write({'last_rebuild_fingerprint': 'abc123'})
        self.assertEqual(inst.last_rebuild_fingerprint, 'abc123')

    # ── Smart rebuild decision ──

    def test_needs_full_rebuild_when_no_last(self):
        """First rebuild should always be full."""
        inst = self._inst()
        self.assertFalse(inst.last_rebuild_fingerprint)
        self.assertNotEqual(
            inst.rebuild_fingerprint,
            inst.last_rebuild_fingerprint,
        )

    def test_needs_cached_rebuild_when_matching(self):
        """When fingerprints match, cached rebuild is sufficient."""
        inst = self._inst()
        inst.write({
            'last_rebuild_fingerprint': inst.rebuild_fingerprint,
        })
        self.assertEqual(
            inst.rebuild_fingerprint,
            inst.last_rebuild_fingerprint,
        )

    def test_needs_full_after_pip_change(self):
        """After changing pip, fingerprints diverge."""
        inst = self._inst()
        inst.write({
            'last_rebuild_fingerprint': inst.rebuild_fingerprint,
        })
        inst.write({'pip_dependencies': 'newpkg==1.0'})
        self.assertNotEqual(
            inst.rebuild_fingerprint,
            inst.last_rebuild_fingerprint,
        )
