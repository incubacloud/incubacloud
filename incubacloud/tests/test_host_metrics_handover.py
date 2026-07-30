"""The handover from the SSH probe to metrics must be complete.

``host_metrics`` (one SSH job per host, every five minutes, on the
``root.bg`` channel) is retired by becoming a no-op while metrics are
flowing. That is the safe shape — if the metrics stack stops, the SSH
path resumes on its own — but it only works if the metrics path really
does everything the SSH path did.

It did not. The SSH probe also owned ``cloud.host.status``: it flipped a
host to ``degraded`` above the disk threshold and back to ``compatible``
below it. Nothing on the metrics side wrote that field, and the SSH job
returns early as soon as readings are fresh — so a host degraded by the
last probe before the cutover stayed degraded permanently, and
``select_best_host`` places instances only on ``compatible`` hosts. The
host would quietly leave the pool with no alert and no log line.

These tests pin both directions of that transition.
"""
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.host_metrics_executor import (
    DISK_CRITICAL_THRESHOLD,
)

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"

_GIB = 1024 ** 3


class HostMetricsHandoverCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
        })
        self.host = self.env["cloud.host"].create({
            "name": "HM",
            "ip_address": "10.0.0.20",
            "user": "root",
            "wildcard_domain": "hm.example.com",
        })

    def _refresh_with_disk_usage(self, percent):
        """Run the specs cron against a backend reporting *percent* used.

        :param percent: root-filesystem usage to simulate.
        """
        size = 100 * _GIB
        free = size * (100 - percent) / 100
        by_expression = {
            "cpu_cores": 4.0,
            "ram_total_gb": 8.0 * _GIB,
            "disk_free_gb": free,
            "_disk_size": size,
        }

        def fake_get(url, params=None, timeout=None, **kwargs):
            expression = (params or {}).get("query", "")
            if "node_cpu_seconds_total" in expression:
                value = by_expression["cpu_cores"]
            elif "MemTotal" in expression:
                value = by_expression["ram_total_gb"]
            elif "avail_bytes" in expression:
                value = by_expression["disk_free_gb"]
            else:
                value = by_expression["_disk_size"]
            resp = MagicMock(spec=requests.Response)
            resp.raise_for_status.return_value = None
            resp.json.return_value = {
                "status": "success",
                "data": {"result": [{
                    "metric": {"host_id": str(self.host.id)},
                    "value": [0, str(value)],
                }]},
            }
            return resp

        with patch(f"{_MODULE}.requests.get", side_effect=fake_get):
            self.env["cloud.host"]._cron_refresh_specs_from_metrics()

    def _disk_alerts(self):
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", "disk_critical"),
            ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])


class TestDiskStatusFollowsMetrics(HostMetricsHandoverCase):

    def test_recovered_disk_restores_the_host_to_the_pool(self):
        """The bug: degraded by SSH, never restored once metrics take over."""
        self.host.write({"status": "degraded"})
        self._refresh_with_disk_usage(30)
        self.assertEqual(
            self.host.status, "compatible",
            "a host whose disk recovered stayed 'degraded', so "
            "select_best_host would never place on it again",
        )

    def test_full_disk_degrades_the_host(self):
        """Above the threshold the host must leave the placement pool."""
        self.host.write({"status": "compatible"})
        self._refresh_with_disk_usage(DISK_CRITICAL_THRESHOLD + 2)
        self.assertEqual(self.host.status, "degraded")

    def test_an_unprobed_host_is_not_degraded(self):
        """Parity with the SSH probe: only 'compatible' escalates.

        A host still in ``unknown`` has not passed setup, so its status
        is not ours to decide — the setup job owns that transition.
        """
        self.assertEqual(self.host.status, "unknown")
        self._refresh_with_disk_usage(DISK_CRITICAL_THRESHOLD + 2)
        self.assertEqual(self.host.status, "unknown")

    def test_a_standing_ssh_era_alert_is_resolved(self):
        """Nothing else would ever clear it — the rule uses another code."""
        self.env["cloud.alert"].sudo().raise_alert(
            "disk_critical", "Disk at 99%", host=self.host, level="critical",
        )
        self._refresh_with_disk_usage(20)
        self.assertFalse(
            self._disk_alerts(),
            "the legacy disk_critical alert outlived the SSH probe that "
            "was the only thing able to resolve it",
        )

    def test_an_unsupported_host_is_not_promoted(self):
        """Only the degraded↔compatible pair is ours to flip.

        A host marked ``unsupported`` failed its setup; a healthy disk
        does not make it deployable.
        """
        self.host.write({"status": "unsupported"})
        self._refresh_with_disk_usage(10)
        self.assertEqual(self.host.status, "unsupported")

    def test_specs_are_written_from_metrics(self):
        """The readings themselves still land — the point of the cron."""
        self._refresh_with_disk_usage(50)
        self.assertEqual(self.host.cpu_cores, 4)
        self.assertEqual(self.host.ram_total_gb, 8.0)
        self.assertEqual(self.host.disk_usage, 50.0)
        self.assertTrue(self.host.last_probed)
