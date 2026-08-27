"""An archived instance is a promise: the copy is there and it can come back.

Archiving is only worth more than deleting if that promise holds, and
the two ways it breaks are both silent. The copy can disappear
underneath it — a provider lifecycle rule or a manual delete empties the
prefix while the credentials and the bucket stay perfectly healthy, so
every "is the backend reachable" check still passes. And the path can
stop pointing at the copy, because the computed one is derived from the
project and the name, either of which can move after archiving.

Neither shows up until someone presses "revive" and gets an empty
instance, which is the worst possible moment to find out. What these
pin is that the record either tells the truth or refuses.
"""
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud


class _ArchivedBase(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "ar-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "ar-host", "ip_address": "10.0.11.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "ar.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "ar-backend",
            "backend_type": "s3",
            "s3_bucket": "ar-bucket",
            "s3_path": "backups",
            "s3_access_key_id": "AKIAAR",
            "s3_secret_access_key": "shh",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "ar-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed",
            "backup_backend_id": self.backend.id,
        })

    def _archive(self):
        self.instance._finalize_removal(keep_in_panel=True)
        return self.instance

    def _measuring(self, total=1024.0, count=7, exc=None):
        """Patch the prefix measurement — no test reaches a real bucket."""
        if exc is not None:
            return patch.object(
                type(self.backend), "_measure_prefix", side_effect=exc,
            )
        return patch.object(
            type(self.backend), "_measure_prefix",
            return_value=(total, count),
        )


class TestArchivedCopyVerification(_ArchivedBase):

    def test_a_present_copy_is_stamped_with_its_size(self):
        inst = self._archive()
        with self._measuring(total=2048.0, count=3):
            self.env["cloud.instance"]._cron_verify_archived_copies()
        self.assertEqual(inst.archive_copy_state, "present")
        self.assertEqual(inst.archive_copy_bytes, 2048.0)
        self.assertTrue(inst.archive_copy_checked_at)

    def test_an_emptied_prefix_is_missing_and_alerts(self):
        """The failure the backend's own health check cannot see: the
        credentials and the bucket are fine, the objects are not."""
        inst = self._archive()
        with self._measuring(total=0.0, count=0):
            self.env["cloud.instance"]._cron_verify_archived_copies()
        self.assertEqual(inst.archive_copy_state, "missing")
        alert = self.env["cloud.alert"].sudo().search([
            ("code", "=", "archive_copy_lost"),
            ("instance_id", "=", inst.id),
            ("state", "=", "active"),
        ])
        self.assertTrue(alert)
        self.assertEqual(alert.level, "critical")

    def test_a_provider_error_is_unreachable_not_missing(self):
        """A network blip must not be reported as data loss: one is
        waited out, the other means the copy is gone for good."""
        inst = self._archive()
        with self._measuring(exc=OSError("connection reset")):
            self.env["cloud.instance"]._cron_verify_archived_copies()
        self.assertEqual(inst.archive_copy_state, "unreachable")
        self.assertFalse(self.env["cloud.alert"].sudo().search([
            ("code", "=", "archive_copy_lost"),
            ("instance_id", "=", inst.id),
            ("state", "=", "active"),
        ]))

    def test_a_copy_that_comes_back_resolves_the_alert(self):
        inst = self._archive()
        with self._measuring(total=0.0, count=0):
            self.env["cloud.instance"]._cron_verify_archived_copies()
        with self._measuring(total=512.0, count=2):
            self.env["cloud.instance"]._cron_verify_archived_copies()
        self.assertFalse(self.env["cloud.alert"].sudo().search([
            ("code", "=", "archive_copy_lost"),
            ("instance_id", "=", inst.id),
            ("state", "=", "active"),
        ]))

    def test_live_instances_are_not_probed(self):
        """The cron is about archived records; measuring the fleet would
        put a bucket call behind every instance, daily."""
        with self._measuring() as measure:
            self.env["cloud.instance"]._cron_verify_archived_copies()
        self.assertEqual(measure.call_count, 0)


class TestRevive(_ArchivedBase):

    def _enqueued(self):
        return patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[1, 2],
        )

    def test_reviving_redeploys_then_restores(self):
        inst = self._archive()
        with self._measuring(), self._enqueued() as chain:
            inst.revive()
        steps = chain.call_args[0][0]
        self.assertEqual(
            [s["job_type_code"] for s in steps][-1], "backup_restore",
        )
        self.assertEqual(steps[-1]["payload"], {"time": "latest"})

    def test_reviving_un_archives_the_record_first(self):
        """Every downstream query filters on ``active``; a still-archived
        record would have its own deploy skip it."""
        inst = self._archive()
        with self._measuring(), self._enqueued():
            inst.revive()
        self.assertTrue(inst.active)

    def test_reviving_onto_another_host_uses_it(self):
        """The original host may be gone, full, or the reason for
        archiving in the first place."""
        other = self.env["cloud.host"].create({
            "name": "ar-host-2", "ip_address": "10.0.11.2", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "ar2.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        inst = self._archive()
        with self._measuring(), self._enqueued() as chain:
            inst.revive(host_id=other.id)
        self.assertTrue(
            all(s["host_id"] == other.id for s in chain.call_args[0][0]),
        )
        self.assertEqual(inst.host_id, other)

    def test_reviving_refuses_when_the_copy_is_gone(self):
        """Checked live, not from the cron's stamp: a deploy that
        succeeds and a restore that finds nothing leaves an empty
        instance where the operator expected their data."""
        inst = self._archive()
        with self._measuring(total=0.0, count=0), self.assertRaises(UserError):
            inst.revive()
        self.assertFalse(inst.active, "a refused revive un-archived it")

    def test_reviving_refuses_when_the_storage_does_not_answer(self):
        inst = self._archive()
        with self._measuring(exc=OSError("boom")), self.assertRaises(UserError):
            inst.revive()

    def test_reviving_a_live_instance_is_refused(self):
        with self.assertRaises(UserError):
            self.instance.revive()

    def test_reviving_without_a_copy_is_refused(self):
        """Archived with no destination: the containers took everything."""
        self.instance.backup_backend_id = False
        self.project.backup_backend_id = False
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.instance.invalidate_recordset()
        inst = self._archive()
        self.assertFalse(inst.custom_backup_dst)
        with self.assertRaises(UserError):
            inst.revive()


class TestArchivedNameIsReserved(_ArchivedBase):
    """The constraint already refuses it; this is about saying why.

    The archived instance's chain lives at a path built from
    ``(project, name)``, so a namesake would write into it. Refusing is
    right — but the raw integrity error names a record that is invisible
    in every list, which reads as a panel bug rather than a decision.
    """

    def test_the_message_names_the_archived_instance(self):
        inst = self._archive()
        with self.assertRaises(UserError) as caught:
            self.env["cloud.instance"].create({
                "name": inst.name,
                "project_id": self.project.id,
                "environment": "staging",
            })
        self.assertIn(inst.name, str(caught.exception))

    def test_another_project_may_reuse_the_name(self):
        """The reservation is per project, like the path."""
        inst = self._archive()
        other = self.env["cloud.project"].create({"name": "ar-proj-2"})
        twin = self.env["cloud.instance"].create({
            "name": inst.name,
            "project_id": other.id,
            "environment": "staging",
        })
        self.assertTrue(twin.exists())


class TestArchivedRoutePayload(_ArchivedBase):
    """What the panel is allowed to claim about an archived copy.

    The list is rendered from this payload alone — it never measures a
    prefix while drawing a row, because that would be one network call
    per line every time someone opened the tab. So whatever the view can
    say about a copy has to travel here, timestamp included: a state
    without its reading date is a claim the panel cannot back up.
    """

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]

    def _fake_request(self):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        return fake_req

    def _payload(self):
        with patch.object(_routes_crud, "request", self._fake_request()):
            return self.controller.cloud_get_archived_instances(
                project_id=self.project.id,
            )

    def test_a_live_instance_is_not_in_the_archived_list(self):
        res = self._payload()
        self.assertTrue(res["ok"])
        self.assertEqual(res["instances"], [])

    def test_the_verification_stamp_travels_with_the_row(self):
        inst = self._archive()
        with self._measuring(total=4096.0, count=5):
            self.env["cloud.instance"]._cron_verify_archived_copies()
        rows = self._payload()["instances"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["copy_state"], "present")
        self.assertEqual(row["copy_bytes"], 4096.0)
        self.assertTrue(row["copy_checked_at"])
        self.assertTrue(row["has_copy"])
        self.assertEqual(row["backup_dst"], inst.custom_backup_dst)

    def test_an_unverified_row_reports_no_state_rather_than_a_wrong_one(self):
        """Freshly archived, before the cron has looked: the payload says
        nothing about the copy instead of guessing it is fine."""
        self._archive()
        row = self._payload()["instances"][0]
        self.assertEqual(row["copy_state"], "")
        self.assertFalse(row["copy_checked_at"])
        # …but the destination is already frozen, so the row is revivable.
        self.assertTrue(row["has_copy"])

    def test_the_archiving_date_survives_the_daily_check(self):
        """It used to be ``write_date``, which the copy-check cron
        rewrites on every archived record — so every archived instance
        reported "archived today", every day, for ever."""
        inst = self._archive()
        archived_on = self._payload()["instances"][0]["archived_on"]
        self.assertTrue(inst.archived_at)
        with self._measuring(total=1.0, count=1):
            self.env["cloud.instance"]._cron_verify_archived_copies()
        self.assertEqual(
            self._payload()["instances"][0]["archived_on"], archived_on,
        )

    def test_the_row_names_the_host_it_was_archived_from(self):
        self._archive()
        row = self._payload()["instances"][0]
        self.assertEqual(row["host_name"], self.host.name)

    def test_below_manager_the_row_does_not_name_the_bucket(self):
        """SEC-009's rule, and the reason for it: in SaaS that storage is
        ours, not the tenant's. A stakeholder still sees that a copy
        exists and what it costs — just not where it is."""
        self._archive()
        with patch.object(
            type(self.env["cloud.security.mixin"]),
            "_has_cloud_group", return_value=False,
        ):
            row = self._payload()["instances"][0]
        self.assertEqual(row["backup_dst"], "")
        self.assertEqual(row["backup_backend_name"], "")
        # …but the useful, non-locating parts survive.
        self.assertTrue(row["has_copy"])
        self.assertEqual(row["name"], self.instance.name)

    def test_a_manager_still_sees_where_the_copy_is(self):
        inst = self._archive()
        with patch.object(
            type(self.env["cloud.security.mixin"]),
            "_has_cloud_group", return_value=True,
        ):
            row = self._payload()["instances"][0]
        self.assertEqual(row["backup_dst"], inst.custom_backup_dst)
        self.assertEqual(row["backup_backend_name"], self.backend.name)

    def test_an_unknown_project_is_an_error_not_an_empty_list(self):
        with patch.object(_routes_crud, "request", self._fake_request()):
            res = self.controller.cloud_get_archived_instances(
                project_id=0,
            )
        self.assertFalse(res["ok"])
