"""The config snapshot is frozen: changing it re-anchors the whole fleet.

``config_dirty`` compares a hash of :meth:`_render_config_snapshot`
against the one the last deploy stamped, so any edit to what that render
emits moves **every** instance's hash at once, with nobody having
touched a thing. It has happened twice — ``5fcc2c5`` (a new
``backup_backend_password`` answer) and ``1f3b23e`` (observability) — and
both times the fleet lit its "Changes not deployed" pill until each
instance was rebuilt or re-anchored by hand.

So this test is a gate, not a description: when it goes red the change
may still be right, but the release carrying it owes the fleet a
re-anchor. Update the frozen sets below in the same commit.

Keys, not values: values legitimately differ between a CI database with
nothing in it and one with real hosts and backends, while the key set is
the thing a code change actually moves.
"""
from odoo.tests.common import TransactionCase

_ANSWER_KEYS = frozenset({
    "project_author", "project_license", "project_name",
    "odoo_version", "odoo_initial_lang", "odoo_admin_password",
    "odoo_proxy",
    "postgres_version", "postgres_dbname", "postgres_username",
    "postgres_password",
    "domains_prod", "domains_test",
    "smtp_relay_host", "smtp_relay_port", "smtp_relay_version",
    "smtp_relay_user", "smtp_relay_password", "smtp_default_from",
    "smtp_canonical_default", "smtp_canonical_domains",
    "backup_dst", "backup_image_version",
    "backup_email_from", "backup_email_to", "backup_smtp_report_success",
    "backup_deletion", "backup_tz",
    "backup_aws_access_key_id", "backup_aws_secret_access_key",
    "backup_passphrase", "backup_backend_password",
})

_EXTRA_FIELDS = frozenset({
    "odoo_conf",
    "odoo_memory_limit", "odoo_cpus",
    "db_memory_limit", "db_cpus",
    "backup_memory_limit", "backup_cpus",
    "smtp_memory_limit", "smtp_cpus",
    "pip_dependencies", "apt_dependencies",
    "odoo_commit_sha",
})

_SNAPSHOT_SECTIONS = frozenset({"answers", "repos", "extras"})

_REANCHOR = (
    "The config snapshot changed shape. Every instance's "
    "applied_config_hash moves with it, so the whole fleet turns dirty "
    "the moment this ships. Update the frozen set here AND make the "
    "release re-anchor the fleet (only migration 1.0.20 ever did)."
)


class TestConfigSnapshotFrozen(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Frozen"})
        self.host = self.env["cloud.host"].create({
            "name": "frozen-host",
            "ip_address": "192.0.2.65",
            "user": "ubuntu",
            "wildcard_domain": "frozen.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "frozeninst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def test_answer_keys_are_frozen(self):
        rendered = set(self.instance._render_copier_answers())
        self.assertEqual(rendered, set(_ANSWER_KEYS), _REANCHOR)

    def test_extra_fields_are_frozen(self):
        declared = set(self.instance._CONFIG_SNAPSHOT_EXTRA_FIELDS)
        self.assertEqual(declared, set(_EXTRA_FIELDS), _REANCHOR)

    def test_snapshot_sections_are_frozen(self):
        snapshot = self.instance._render_config_snapshot()
        self.assertEqual(set(snapshot), set(_SNAPSHOT_SECTIONS), _REANCHOR)

    def test_backup_branch_only_fills_existing_keys(self):
        """A configured backend must not add keys, only values.

        The answers dict declares every backup key up front precisely so
        that enabling a backend on one instance does not give it a
        different snapshot shape from its neighbours.
        """
        without = set(self.instance._render_copier_answers())
        backend = self.env["cloud.backup.backend"].create({
            "name": "Frozen backend",
            "backend_type": "s3",
            "s3_bucket": "frozen-bucket",
            "s3_access_key_id": "AKIAFROZEN",
            "s3_secret_access_key": "secret",
        })
        self.instance.write({"backup_backend_id": backend.id})
        self.instance.invalidate_recordset()
        # Guard against a vacuous pass: without a resolvable backup_dst
        # the branch under test never runs.
        self.assertTrue(self.instance._backup_enabled())
        self.assertEqual(
            set(self.instance._render_copier_answers()), without, _REANCHOR,
        )
