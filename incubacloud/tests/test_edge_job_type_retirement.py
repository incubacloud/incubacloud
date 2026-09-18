"""The allowlist's job type must not take an upgrade down with it.

Core 1.0.125 stops declaring ``push_github_webhook_edge``, and the end of
an upgrade deletes whatever a module no longer declares. ``job_type_id``
is a required many2one, so the delete is refused while any job points at
the type — and that refusal aborts the whole upgrade. The migration keeps
the type out of the cleanup exactly while history still uses it.
"""
import importlib.util
import os

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

_XMLID_NAME = "push_github_webhook_edge"


def _load_migration():
    """Import the 1.0.125 pre-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.125", "pre-migrate.py",
    )
    spec = importlib.util.spec_from_file_location("ic_pre_migrate_1_0_125", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEdgeJobTypeRetirement(TransactionCase):
    """A type still referenced survives the cleanup; an unused one does not."""

    def setUp(self):
        """Recreate core's old identifier for a type of the retired kind."""
        super().setUp()
        self.job_type = self.env["cloud.job.type"].create({
            "name": "Retired type",
            "code": "retired_edge_type_under_test",
            "apply_to": "host",
        })
        self.imd = self.env["ir.model.data"].create({
            "module": "incubacloud",
            "name": _XMLID_NAME,
            "model": "cloud.job.type",
            "res_id": self.job_type.id,
            "noupdate": False,
        })
        self.migration = _load_migration()

    def _noupdate(self):
        """Return the flag as stored, bypassing the ORM cache."""
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT noupdate FROM ir_model_data WHERE id = %s", (self.imd.id,),
        )
        return self.env.cr.fetchone()[0]

    def test_a_type_with_jobs_is_kept_out_of_the_cleanup(self):
        """One job of the type is enough to make its deletion abort."""
        host = self.env["cloud.host"].create({
            "name": "retirement-host",
            "ip_address": "192.0.2.90",
            "user": "ubuntu",
            "wildcard_domain": "retirement.example.com",
        })
        self.env["cloud.job"].create({
            "host_id": host.id,
            "job_type_id": self.job_type.id,
            "name": "old allowlist push",
        })
        self.migration.migrate(self.env.cr, "1.0.124")
        self.assertTrue(self._noupdate())

    def test_an_unused_type_is_left_to_be_deleted(self):
        """Without history nothing blocks the delete, so nothing is kept."""
        self.migration.migrate(self.env.cr, "1.0.124")
        self.assertFalse(self._noupdate())
