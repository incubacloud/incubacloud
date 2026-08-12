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


class TestRebuildReanchorsDrift(TransactionCase):
    """A successful rebuild must re-anchor ``applied_config_hash``.

    Only the deploy executor anchored before; a drift-curing rebuild
    left the pill lit forever."""

    def test_rebuild_on_success_writes_the_anchor(self):
        import asyncio

        from odoo.addons.incubacloud.models.rebuild_instance_executor import (
            RebuildInstanceExecutor,
        )

        env = self.env
        jt = env["cloud.job.type"].search(
            [("code", "=", "rebuild_instance")], limit=1,
        ) or env["cloud.job.type"].create(
            {"name": "rebuild", "code": "rebuild_instance",
             "apply_to": "instance"},
        )
        host = env["cloud.host"].create(
            {"name": "reanchor-host", "ip_address": "192.0.2.41",
             "user": "ubuntu", "wildcard_domain": "ra.example.com"},
        )
        project = env["cloud.project"].create({"name": "Reanchor Proj"})
        inst = env["cloud.instance"].create(
            {"name": "reanchorinst", "project_id": project.id,
             "environment": "production", "host_id": host.id},
        )
        job = env["cloud.job"].create(
            {"host_id": host.id, "instance_id": inst.id,
             "job_type_id": jt.id, "name": "Rebuild"},
        )
        executor = RebuildInstanceExecutor(job, host)
        self.assertFalse(inst.applied_config_hash)
        asyncio.run(executor.on_success({}))
        self.assertEqual(
            inst.applied_config_hash, inst._config_snapshot_hash(),
        )


class TestTemplateDrift(TransactionCase):
    """Tier 3 — template drift: saved config vs what this version ships.

    A different question from ``config_dirty``, and the reason it exists:
    a host can be perfectly in sync with what the last full setup shipped
    and still be running a Traefik config from before a feature was added
    to the seed templates. That is exactly how the JSON access log reached
    no provisioned host for months while every drift signal read clean.
    """

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]
        self.host = self.Host.create(
            {
                "name": "tpl-drift-host",
                "ip_address": "192.0.2.42",
                "user": "ubuntu",
                "wildcard_domain": "tpl.example.com",
            }
        )

    def _drift(self):
        self.host.invalidate_recordset(
            ["template_drift", "template_drift_details"],
        )
        return self.host

    # ── The walker ─────────────────────────────────────────────────────

    def test_reports_a_key_the_stored_copy_lacks(self):
        missing = self.Host._missing_template_paths(
            {"a": {"b": 1, "c": 2}}, {"a": {"b": 1}},
        )
        self.assertEqual(missing, ["a.c"])

    def test_ignores_a_different_value(self):
        """A host's own domain, e-mail or port is not drift."""
        missing = self.Host._missing_template_paths(
            {"acme": {"email": "seed@example.com"}},
            {"acme": {"email": "ops@customer.com"}},
        )
        self.assertEqual(missing, [])

    def test_ignores_extra_keys_the_host_added(self):
        missing = self.Host._missing_template_paths(
            {"a": 1}, {"a": 1, "plugins": {"sablier": {}}},
        )
        self.assertEqual(missing, [])

    def test_reports_a_whole_missing_block(self):
        missing = self.Host._missing_template_paths(
            {"accessLog": {"format": "json"}}, {},
        )
        self.assertEqual(missing, ["accessLog"])

    # ── Equivalences ───────────────────────────────────────────────────

    def test_an_equivalent_spelling_is_not_drift(self):
        """``filename`` and ``directory`` are the same file provider.

        A layer that needs to drop extra dynamic files switches to the
        directory form. Flagging that would leave a warning nobody can
        clear, which is how a drift signal stops being read at all.
        """
        self.host.traefik_yml = self.host.traefik_yml.replace(
            "filename: /etc/traefik/config.yml",
            "directory: /etc/traefik/dynamic",
        )
        details = self._drift().template_drift_details or ""
        self.assertNotIn("providers.file.filename", details)

    def test_the_equivalence_only_covers_its_own_pair(self):
        """Dropping the provider outright is still drift."""
        self.host.traefik_yml = self.host.traefik_yml.replace(
            "    filename: /etc/traefik/config.yml\n", "",
        )
        self.assertTrue(self._drift().template_drift)

    # ── Behaviour on records ───────────────────────────────────────────

    def test_a_host_seeded_from_the_templates_is_clean(self):
        """The guard: this fails the day a template gains a setting and
        no retrofit is wired to carry it to existing hosts.

        That omission is precisely what left every host without an access
        log, silently, and nothing in the system could notice."""
        self.Host.init_traefik_templates()
        self.host.invalidate_recordset()
        self.assertFalse(
            self._drift().template_drift,
            "A host built from the shipped templates reports drift: a "
            "template declares something init_traefik_templates does not "
            "retrofit. Add the merge, do not relax this test.",
        )

    def test_a_stale_stored_copy_is_flagged_and_named(self):
        self.host.traefik_yml = "global:\n  sendAnonymousUsage: false\n"
        host = self._drift()
        self.assertTrue(host.template_drift)
        self.assertIn("traefik.yml: accessLog", host.template_drift_details)

    def test_the_access_log_retrofit_clears_it(self):
        self.host.traefik_yml = self.host.traefik_yml.replace(
            "accessLog:", "accessLogDisabled:",
        )
        self.assertTrue(self._drift().template_drift)
        self.Host.init_traefik_templates()
        self.host.invalidate_recordset()
        self.assertFalse(self._drift().template_drift)

    def test_unparseable_yaml_does_not_break_the_page(self):
        """An operator's editing mistake is a different problem."""
        self.host.traefik_yml = "global:\n  broken: [unclosed\n"
        self.assertFalse(self._drift().template_drift)
