"""Tier 2 — config drift: saved config vs what the last deploy shipped.

The snapshot hash covers the full copier answers plus the token-free
repos description, so ANY deploy-feeding field edited after the anchor
must flag the record dirty; a fresh anchor (what a successful deploy
writes) must clear it. Host-side: same mechanism against full_setup.
"""
from odoo.tests.common import TransactionCase


class TestConfigDrift(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Drift Proj"})
        self.host = self.env["cloud.host"].create(
            {
                "name": "drift-host",
                "ip_address": "192.0.2.40",
                "user": "ubuntu",
                "wildcard_domain": "drift.example.com",
            }
        )
        self.instance = self.env["cloud.instance"].create(
            {
                "name": "driftinst",
                "project_id": self.project.id,
                "environment": "production",
                "host_id": self.host.id,
            }
        )

    def _dirty(self, record):
        """Read ``config_dirty`` through a fresh compute.

        The compute intentionally depends only on the anchor (the
        snapshot side covers every field automatically, so it has no
        dependency list); within one test env the cached value must be
        invalidated by hand, the way a new HTTP request would start
        with a fresh cache.
        """
        record.invalidate_recordset(["config_dirty"])
        return record.config_dirty

    def _anchor(self, record):
        record.applied_config_hash = record._config_snapshot_hash()
        self.assertFalse(self._dirty(record))

    def test_never_deployed_record_is_not_dirty(self):
        self.assertFalse(self._dirty(self.instance))
        self.assertFalse(self._dirty(self.host))

    def test_snapshot_hash_is_deterministic(self):
        self.assertEqual(
            self.instance._config_snapshot_hash(),
            self.instance._config_snapshot_hash(),
        )

    def test_editing_an_answers_field_flags_dirty(self):
        self._anchor(self.instance)
        self.instance.smtp_relay_host = "mail.drift.example.com"
        self.assertTrue(self._dirty(self.instance))

    def test_editing_an_extras_field_flags_dirty(self):
        self._anchor(self.instance)
        self.instance.pip_dependencies = "requests==2.32.0"
        self.assertTrue(self._dirty(self.instance))

    def test_domain_lifecycle_flags_and_clears(self):
        self._anchor(self.instance)
        domain = self.env["cloud.instance.domain"].create(
            {
                "instance_id": self.instance.id,
                "hostname": "shop.drift.example.com",
            }
        )
        self.assertTrue(self._dirty(self.instance))
        self._anchor(self.instance)
        domain.unlink()
        self.assertTrue(
            self._dirty(self.instance),
            "a deleted domain must count as an unapplied change "
            "(the server still serves it until the next rebuild)",
        )

    def test_snapshot_excludes_tokens_and_template_pin(self):
        snap = self.instance._render_config_snapshot()
        flat = str(snap)
        self.assertNotIn("x-access-token", flat)
        self.assertNotIn("copier_template", flat)

    def test_host_traefik_edit_flags_dirty(self):
        self._anchor(self.host)
        self.host.traefik_yml = (self.host.traefik_yml or "") + "\n# edited"
        self.assertTrue(self._dirty(self.host))

    def test_host_whitelist_change_flags_dirty(self):
        self._anchor(self.host)
        self.env["cloud.host.whitelist"].create(
            {
                "host_id": self.host.id,
                "hostname": "extra.allowed.example.com",
            }
        )
        self.assertTrue(self._dirty(self.host))
