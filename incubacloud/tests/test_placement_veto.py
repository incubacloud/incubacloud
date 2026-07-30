"""Tier 2 — placement veto from measured-overload alerts.

``select_best_host`` scores hosts by *declared* allocation; the veto
keeps it away from hosts the metrics pipeline has actually measured as
overloaded (or silent). With no metrics there are no such alerts and
the selection is unchanged — fail-safe by construction.
"""
from odoo.tests.common import TransactionCase

_HOST_BASE = {"user": "ubuntu", "wildcard_domain": "veto.example.com"}


class TestPlacementVeto(TransactionCase):

    def setUp(self):
        super().setUp()
        Host = self.env["cloud.host"]
        existing = Host.search([])
        if existing:
            existing.write({"exclude_from_autoassign": True})
        self.host_big = Host.create(
            {
                "name": "veto-big",
                "ip_address": "10.0.0.71",
                "cpu_cores": 16,
                "ram_total_gb": 32.0,
                "status": "compatible",
                "traefik_deployed": True,
                "disk_free_gb": 100.0,
            }
            | _HOST_BASE
        )
        self.host_small = Host.create(
            {
                "name": "veto-small",
                "ip_address": "10.0.0.72",
                "cpu_cores": 8,
                "ram_total_gb": 16.0,
                "status": "compatible",
                "traefik_deployed": True,
                "disk_free_gb": 50.0,
            }
            | _HOST_BASE
        )
        self.Host = Host
        self.Alert = self.env["cloud.alert"].sudo()

    def test_overloaded_winner_is_skipped(self):
        """The bigger host would win on declared headroom; a measured
        overload alert hands the placement to the smaller one."""
        self.assertEqual(self.Host.select_best_host(), self.host_big)
        self.Alert.raise_alert(
            "metrics_memory_high", "measured 96% RAM", level="critical",
            host=self.host_big,
        )
        self.assertEqual(self.Host.select_best_host(), self.host_small)

    def test_all_vetoed_still_places(self):
        """When every candidate is vetoed the veto steps aside —
        placement keeps working and the alerts remain the signal."""
        for host in (self.host_big, self.host_small):
            self.Alert.raise_alert(
                "metrics_cpu_saturated", "measured load", level="critical",
                host=host,
            )
        self.assertEqual(self.Host.select_best_host(), self.host_big)

    def test_resolved_alert_lifts_the_veto(self):
        self.Alert.raise_alert(
            "metrics_memory_high", "measured 96% RAM", level="critical",
            host=self.host_big,
        )
        self.Alert.resolve_alert("metrics_memory_high", host=self.host_big)
        self.assertEqual(self.Host.select_best_host(), self.host_big)

    def test_unrelated_alert_does_not_veto(self):
        self.Alert.raise_alert(
            "job_running_too_long", "some job", level="warning",
            host=self.host_big,
        )
        self.assertEqual(self.Host.select_best_host(), self.host_big)
