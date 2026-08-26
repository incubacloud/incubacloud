"""Tier 2 — deleting a backup backend must see every route to it.

The old guard asked one question: is any *active* instance assigned to
this backend directly? An instance actually resolves its destination as
``instance.backup_backend_id or project.backup_backend_id or <global
default>``, so two whole routes answered "nobody" and the global default
— the backend the entire fleet inherits — could be deleted without a
single warning.

Archived instances are the third hole and the one this repository is
about to widen: archiving keeps the record precisely because its chain
is still in the bucket, and ``active_test`` hides exactly those records
from the search the guard used.

Deleting the backend never deletes an object in S3. What it destroys is
the panel's only pointer to them, which is why the guard has to name
what it is protecting rather than just refusing.
"""
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_backends
from odoo.addons.incubacloud.models.cloud_instance import _GLOBAL_BACKUP_PARAM


class TestBackupBackendDeleteGuard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Backend = self.env["cloud.backup.backend"]
        self.backend = self.Backend.create({
            "name": "Guarded Store",
            "backend_type": "s3",
            "s3_bucket": "guarded",
        })
        self.project = self.env["cloud.project"].create({"name": "Guard-proj"})
        self.controller = _routes_backends.BackendsMixin()
        # Same stub as test_backup_backend_conn: the mixin is exercised in
        # isolation, and TransactionCase runs superuser so the gate passes.
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        # Neutralise whatever the database carries, so a global default
        # left behind by another fixture cannot make these tests pass or
        # fail for the wrong reason.
        self.env["ir.config_parameter"].sudo().set_param(
            _GLOBAL_BACKUP_PARAM, "0",
        )

    # ── harness ────────────────────────────────────────────────────

    def _delete(self):
        """Call the delete endpoint with a stubbed ``request``."""
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_backends, "request", fake_req):
            return self.controller.cloud_delete_backup_backend(
                self.backend.id,
            )

    def _instance(self, name, **vals):
        """Create a production instance in the fixture project.

        :param name: instance name, unique within the project.
        :param vals: extra values merged over the defaults.
        :return: the new ``cloud.instance`` record.
        """
        return self.env["cloud.instance"].create({
            "name": name,
            "project_id": self.project.id,
            "environment": "production",
        } | vals)

    def _set_global_default(self):
        """Make the fixture backend the fleet-wide default."""
        self.env["ir.config_parameter"].sudo().set_param(
            _GLOBAL_BACKUP_PARAM, str(self.backend.id),
        )

    # ── what the guard already saw ─────────────────────────────────

    def test_an_active_instance_still_blocks(self):
        self._instance("guard-active", backup_backend_id=self.backend.id)
        result = self._delete()
        self.assertFalse(result["ok"])
        self.assertIn("guard-active", result["error"])
        self.assertTrue(self.backend.exists())

    def test_an_unused_backend_is_deleted(self):
        result = self._delete()
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.backend.exists())

    # ── the three holes ────────────────────────────────────────────

    def test_an_archived_instance_blocks_and_says_so(self):
        """The archive keeps the record *because* the chain survives."""
        inst = self._instance("guard-archived", backup_backend_id=self.backend.id)
        inst.write({"active": False})
        result = self._delete()
        self.assertFalse(result["ok"])
        self.assertIn("guard-archived", result["error"])
        self.assertIn("archived", result["error"])
        self.assertTrue(self.backend.exists())

    def test_a_project_default_blocks_and_names_the_project(self):
        self.project.write({"backup_backend_id": self.backend.id})
        result = self._delete()
        self.assertFalse(result["ok"])
        self.assertIn("Guard-proj", result["error"])
        self.assertTrue(self.backend.exists())

    def test_the_global_default_blocks(self):
        """The worst case: nothing points at it and everything uses it."""
        self._set_global_default()
        result = self._delete()
        self.assertFalse(result["ok"])
        self.assertIn("global default", result["error"])
        self.assertTrue(self.backend.exists())

    # ── the message has to be actionable ───────────────────────────

    def test_every_reason_is_reported_at_once(self):
        """One round-trip per hole would be three rounds of guessing."""
        self._instance("guard-multi", backup_backend_id=self.backend.id)
        self.project.write({"backup_backend_id": self.backend.id})
        self._set_global_default()
        result = self._delete()
        self.assertFalse(result["ok"])
        for fragment in ("guard-multi", "Guard-proj", "global default"):
            self.assertIn(fragment, result["error"])

    def test_the_message_does_not_claim_the_backups_are_deleted(self):
        """Deleting the record unlinks a pointer, not a single object."""
        self._instance("guard-copy", backup_backend_id=self.backend.id)
        result = self._delete()
        self.assertIn("not delete the backups", result["error"])

    # ── the model helper ───────────────────────────────────────────

    def test_blockers_reports_each_route_separately(self):
        inst = self._instance("guard-b", backup_backend_id=self.backend.id)
        inst.write({"active": False})
        self.project.write({"backup_backend_id": self.backend.id})
        self._set_global_default()
        blockers = self.backend._deletion_blockers()
        self.assertEqual(blockers["instances"], inst)
        self.assertEqual(blockers["projects"], self.project)
        self.assertTrue(blockers["is_global_default"])

    def test_blockers_does_not_claim_another_backends_global_default(self):
        other = self.Backend.create({
            "name": "Other Store",
            "backend_type": "s3",
            "s3_bucket": "other",
        })
        self.env["ir.config_parameter"].sudo().set_param(
            _GLOBAL_BACKUP_PARAM, str(other.id),
        )
        self.assertFalse(
            self.backend._deletion_blockers()["is_global_default"],
        )
        self.assertTrue(other._deletion_blockers()["is_global_default"])
