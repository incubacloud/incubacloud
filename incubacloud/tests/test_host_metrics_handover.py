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
import contextlib
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.sql_db import Cursor
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import cloud_host_metrics
from odoo.addons.incubacloud.models.host_metrics_executor import (
    DISK_CRITICAL_THRESHOLD,
)
from odoo.addons.incubacloud.models.host_probe_executor import (
    LOW_DISK_ALERT,
    MISSING_TOOLS_ALERT,
)

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"

_GIB = 1024 ** 3


class HostMetricsHandoverCase(TransactionCase):

    def setUp(self):
        super().setUp()
        # The cron writes on a READ COMMITTED cursor of its own: in test
        # mode it rides on this transaction, outside it would be a real
        # connection that sees nothing of the test and waits for its locks.
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
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

    def _refresh_with_disk_usage(self, percent, size_gb=100):
        """Run the specs cron against a backend reporting *percent* used.

        :param percent: root-filesystem usage to simulate.
        :param size_gb: size of the simulated disk, in GiB.
        """
        size = size_gb * _GIB
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
            self._cron()

    def _cron(self):
        """Run the specs cron as after a commit: what the test wrote is
        flushed first, and the test reads what the cron's own cursor
        wrote afterwards."""
        self.env.flush_all()
        self.env["cloud.host"]._cron_refresh_specs_from_metrics()
        self.env.invalidate_all()

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

    def test_a_good_read_is_stamped_and_a_failed_one_is_not(self):
        self.settings.metrics_read_ok_at = False
        self._refresh_with_disk_usage(40)
        self.settings.invalidate_recordset()
        self.assertTrue(self.settings.metrics_read_ok_at)
        self.settings.metrics_read_ok_at = False
        with patch("requests.get", side_effect=requests.ConnectionError("down")):
            self._cron()
        self.settings.invalidate_recordset()
        self.assertFalse(self.settings.metrics_read_ok_at)

    def test_a_pass_leaves_the_registry_caches_alone(self):
        """The cron runs every five minutes on every panel that reads
        metrics: stamping the read with ``set_param`` would empty the
        ``stable`` caches of every worker each time."""
        self._refresh_with_disk_usage(40)
        # An older stamp: within one second the time would not move at all.
        self.settings.metrics_read_ok_at = fields.Datetime.now() - timedelta(hours=1)
        with patch.object(
            type(self.env.registry), "clear_cache", autospec=True,
        ) as clear:
            self._refresh_with_disk_usage(40)
        clear.assert_not_called()
        self.settings.invalidate_recordset()
        self.assertGreater(
            self.settings.metrics_read_ok_at,
            fields.Datetime.now() - timedelta(minutes=1),
        )

    def test_full_disk_degrades_the_host(self):
        """Above the threshold the host must leave the placement pool."""
        self.host.write({"status": "compatible"})
        self._refresh_with_disk_usage(DISK_CRITICAL_THRESHOLD + 2)
        self.assertEqual(self.host.status, "degraded")

    def _check_found(self, code):
        """Leave the host as a Check that raised *code* left it."""
        self.env["cloud.alert"].sudo().raise_alert(
            code, "found by the Check", host=self.host,
        )
        self.host.write({"status": "degraded"})

    def _active(self, code):
        """Return the host's active alerts *code*."""
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", code),
            ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])

    def test_the_checks_low_disk_keeps_the_host_degraded(self):
        """92 % of 100 GB used leaves 8 GB: under the Check's 10 GB,
        though under the readings' own threshold too. Until 9-nov-2026
        the reading right after the Check put the host back to
        compatible."""
        self._check_found(LOW_DISK_ALERT)
        self._refresh_with_disk_usage(92)
        self.assertEqual(self.host.status, "degraded")
        self.assertTrue(self._active(LOW_DISK_ALERT))

    def test_freed_disk_settles_the_checks_verdict(self):
        """The space was freed: no need to wait for another Check."""
        self._check_found(LOW_DISK_ALERT)
        self._refresh_with_disk_usage(30)
        self.assertFalse(self._active(LOW_DISK_ALERT))
        self.assertEqual(self.host.status, "compatible")

    def test_missing_tools_keep_the_host_degraded(self):
        """A good disk does not install the tools the Check missed."""
        self._check_found(MISSING_TOOLS_ALERT)
        self._refresh_with_disk_usage(30)
        self.assertEqual(self.host.status, "degraded")
        self.assertTrue(self._active(MISSING_TOOLS_ALERT))

    def _dismiss_by_hand(self, code):
        """Close the host's active alert *code* as a user would."""
        self._active(code).write({"state": "dismissed", "resolution": "manual"})

    def test_a_low_disk_dismissed_by_hand_keeps_the_host_degraded(self):
        """92 % of 100 GB leaves 8 GB: dismissing the alert frees none."""
        self._check_found(LOW_DISK_ALERT)
        self._dismiss_by_hand(LOW_DISK_ALERT)
        self._refresh_with_disk_usage(92)
        self.assertEqual(self.host.status, "degraded")

    def test_missing_tools_dismissed_by_hand_keep_the_host_degraded(self):
        """Dismissing the alert installs nothing: a Check or a Full
        Setup that finds the tools is what closes it."""
        self._check_found(MISSING_TOOLS_ALERT)
        self._dismiss_by_hand(MISSING_TOOLS_ALERT)
        self._refresh_with_disk_usage(30)
        self.assertEqual(self.host.status, "degraded")

    def test_missing_tools_resolved_by_a_check_free_the_host(self):
        """The newest missing-tools alert decides: one closed by a later
        Check, after an older one dismissed by hand, leaves the host
        free to the readings."""
        self._check_found(MISSING_TOOLS_ALERT)
        self._dismiss_by_hand(MISSING_TOOLS_ALERT)
        self._check_found(MISSING_TOOLS_ALERT)
        self.env["cloud.alert"].sudo().resolve_alert(
            MISSING_TOOLS_ALERT, host=self.host,
        )
        self._refresh_with_disk_usage(30)
        self.assertEqual(self.host.status, "compatible")

    def test_the_low_disk_alert_closes_one_gigabyte_above_the_threshold(self):
        """89.5 % of 100 GB leaves 10.5 GB: over the Check's 10 GB, but a
        reading rounded to 0.1 GB, and maybe older than the Check, is no
        proof the disk has room. It closes from 11 GB."""
        self._check_found(LOW_DISK_ALERT)
        self._refresh_with_disk_usage(89.5)
        self.assertTrue(self._active(LOW_DISK_ALERT))
        self.assertEqual(self.host.status, "degraded")
        self._refresh_with_disk_usage(88)
        self.assertFalse(self._active(LOW_DISK_ALERT))
        self.assertEqual(self.host.status, "compatible")

    def test_a_full_but_large_disk_settles_the_low_disk_alert(self):
        """96 % of 500 GB leaves 20 GB: the disk is critical, yet not
        short of what the Check asks for, and its low-disk alert must not
        stay lit for as long as the disk stays over 95 %."""
        self._check_found(LOW_DISK_ALERT)
        self._refresh_with_disk_usage(DISK_CRITICAL_THRESHOLD + 1, size_gb=500)
        self.assertFalse(self._active(LOW_DISK_ALERT))
        self.assertEqual(self.host.status, "degraded")

    def test_a_disk_the_readings_degraded_is_restored_under_ten_gb(self):
        """95 % of 100 GB degrades the host; at 92 % (8 GB free), with no
        Check behind it, the readings restore what they degraded."""
        self.host.write({"status": "compatible"})
        self._refresh_with_disk_usage(DISK_CRITICAL_THRESHOLD)
        self.assertEqual(self.host.status, "degraded")
        self._refresh_with_disk_usage(92)
        self.assertEqual(self.host.status, "compatible")

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
            confirm_after=1,  # already standing: the fixture, not a probe
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


#: The table a writing statement writes, whatever the statement.
_WRITE = re.compile(
    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(\w+)\"?",
    re.IGNORECASE,
)


class TestTheCronsTransactionWritesNothing(HostMetricsHandoverCase):
    """Every write of the specs cron is on its READ COMMITTED cursor.

    The host rows it stamps are the ones the durable hooks of
    ``host_metrics``, ``full_setup`` and ``host_hardening`` and a save of
    the host write. Written in the cron's REPEATABLE READ snapshot after
    the PromQL queries, the second writer lost the row and the whole
    pass rolled back, the disk status with it (inventory H7). The rule
    pinned here: the separate cursor only writes rows the cron's own
    transaction does not write, before or after; and since that
    transaction writes nothing at all, no row can be written by both.
    """

    def _writes(self, percent):
        """Run the cron and sort what it wrote by the cursor writing it.

        :return: ``(main, separate)``, the sets of tables written by the
            cron's transaction and by its READ COMMITTED cursor.
        """
        # What the test itself wrote is not the cron's.
        self.env.flush_all()
        main, separate = set(), set()
        inside = []
        real_open = cloud_host_metrics.read_committed_cursor
        real_execute = Cursor.execute

        @contextlib.contextmanager
        def tracked(registry):
            """Open the real cursor and note while it is in use, its
            commit included."""
            inside.append(True)
            try:
                with real_open(registry) as cr:
                    yield cr
            finally:
                inside.pop()

        def spy(cr, query, params=None, *args, **kwargs):
            """Note the table of every write, by the cursor it is on."""
            code = getattr(query, "code", query)
            found = _WRITE.match(code if isinstance(code, str) else "")
            if found:
                (separate if inside else main).add(found.group(1))
            return real_execute(cr, query, params, *args, **kwargs)

        with patch.object(
            cloud_host_metrics, "read_committed_cursor", side_effect=tracked,
        ), patch.object(Cursor, "execute", autospec=True, side_effect=spy):
            self._refresh_with_disk_usage(percent)
            # What the cron's transaction left in its cache would be
            # written by its commit.
            self.env.flush_all()
        return main, separate

    def test_the_hosts_and_the_disk_status_are_written_apart(self):
        self.host.write({"status": "compatible"})
        main, separate = self._writes(DISK_CRITICAL_THRESHOLD + 2)
        self.assertIn("cloud_host", separate)
        self.assertEqual(self.host.status, "degraded")
        self.assertEqual(main, set())
        self.assertFalse(main & separate)

    def test_a_recovered_disk_resolves_its_alert_apart_too(self):
        self.env["cloud.alert"].sudo().raise_alert(
            "disk_critical", "Disk at 99%", host=self.host, level="critical",
            confirm_after=1,
        )
        self.host.write({"status": "degraded"})
        main, separate = self._writes(20)
        self.assertIn("cloud_alert", separate)
        self.assertEqual(self.host.status, "compatible")
        self.assertFalse(self._disk_alerts())
        self.assertEqual(main, set())
