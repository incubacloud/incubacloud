"""The direct list follows the fleet: who comes, who goes, and every hour.

A host created with an address, a changed address, an archived or deleted
host queue ``sync_direct_path`` on every host whose proxy publishes a
direct port. A cron every 15 minutes queues it where the list in force
differs from the one it should be, or the last good run is over an hour
old; and after 30 minutes behind, it says so.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.tests._direct import make_host


class _TriggerCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.publisher = make_host(
            self.env, "tr-central", "46.4.0.80",
            direct_port=9443, direct_port_shipped=9443,
        )
        self.Host = self.env["cloud.host"]

    def _settle(self):
        """Take the waiting syncs off the queue, as a worker running them would.

        ``sync_direct_path`` collapses: a change that finds one waiting
        becomes that one, so the next change only makes a job of its own
        once the waiting one has gone.
        """
        self.env["cloud.job"].search([
            ("job_type_id.code", "=", "sync_direct_path"),
            ("state", "=", "queued"),
        ]).write({"state": "done"})

    def _syncs(self, host=None):
        return self.env["cloud.job"].search_count([
            ("host_id", "=", (host or self.publisher).id),
            ("job_type_id.code", "=", "sync_direct_path"),
        ])


class TestTheFleetMovesTheList(_TriggerCase):

    def test_a_host_created_with_an_address(self):
        before = self._syncs()
        make_host(self.env, "tr-new", "46.4.0.81")
        self.assertGreater(self._syncs(), before)

    def test_a_host_still_without_its_address_changes_nothing(self):
        before = self._syncs()
        make_host(self.env, "tr-bought", "0.0.0.0")
        self.assertEqual(self._syncs(), before)

    def test_a_new_address_an_archived_host_and_a_deleted_one(self):
        host = make_host(self.env, "tr-moving", "46.4.0.82")
        for change in (
            lambda: host.write({"ip_address": "46.4.0.83"}),
            lambda: host.write({"active": False}),
        ):
            self._settle()
            before = self._syncs()
            change()
            self.assertGreater(self._syncs(), before)
        other = make_host(self.env, "tr-gone", "46.4.0.84")
        self._settle()
        before = self._syncs()
        other.unlink()
        self.assertGreater(self._syncs(), before)

    def test_without_a_publishing_host_nothing_is_queued(self):
        self.publisher.direct_port_shipped = 0
        before = self._syncs()
        make_host(self.env, "tr-alone", "46.4.0.85")
        self.assertEqual(self._syncs(), before)

    def test_the_public_path_switch_converges(self):
        cron = self.env.ref("incubacloud.cron_reconcile_observability")
        # A copy of production has it off, and an inactive cron takes no
        # trigger for "now".
        cron.sudo().active = True
        triggers = self.env["ir.cron.trigger"].sudo()
        before = triggers.search_count([("cron_id", "=", cron.id)])
        self.publisher.direct_path_opt_out = True
        self.assertGreater(triggers.search_count([("cron_id", "=", cron.id)]), before)

    def test_a_new_central_name_is_probed_at_once(self):
        settings = self.env["cloud.settings"].sudo()._get_system()
        settings.metrics_central_host_id = self.publisher
        before = self._syncs()
        settings.metrics_central_direct_host = "direct.metrics.example.test"
        self.assertGreater(self._syncs(), before)


class TestTheCron(_TriggerCase):

    def _in_step(self, synced_ago):
        self.publisher.write({
            "direct_allowlist_shipped": "\n".join(self.Host._direct_peers_entries()),
            "direct_allowlist_synced_at": fields.Datetime.now() - synced_ago,
        })

    def test_in_step_and_recent_queues_nothing(self):
        self._in_step(timedelta(minutes=30))
        before = self._syncs()
        self.Host._cron_sync_direct_path()
        self.assertEqual(self._syncs(), before)

    def test_in_step_but_over_an_hour_old_queues_a_run(self):
        self._in_step(timedelta(minutes=61))
        before = self._syncs()
        self.Host._cron_sync_direct_path()
        self.assertGreater(self._syncs(), before)

    def test_a_list_behind_queues_a_run_and_alerts_after_30_minutes(self):
        self._in_step(timedelta(minutes=5))
        make_host(self.env, "tr-late", "46.4.0.86")
        self.Host._cron_sync_direct_path()
        self.assertTrue(self.publisher.direct_allowlist_stale_since)
        Alert = self.env["cloud.alert"].sudo()
        stale = [
            ("code", "=", "direct_allowlist_stale"),
            ("host_id", "=", self.publisher.id),
            ("state", "=", "active"),
        ]
        self.assertFalse(Alert.search(stale))
        self.publisher.direct_allowlist_stale_since = (
            fields.Datetime.now() - timedelta(minutes=31)
        )
        self.Host._cron_sync_direct_path()
        alert = Alert.search(stale)
        self.assertTrue(alert)
        self.assertIn("1 address(es) not admitted yet", alert.message)
        self._in_step(timedelta(minutes=5))
        self.Host._cron_sync_direct_path()
        self.assertFalse(Alert.search(stale))
        self.assertFalse(self.publisher.direct_allowlist_stale_since)
