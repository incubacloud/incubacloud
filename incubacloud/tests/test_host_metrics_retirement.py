"""Tests for retiring the SSH host-metrics probe (Fase 4 / A9).

The retirement is deliberately *conditional*, not a deletion: the SSH job
becomes a no-op only while metrics are actually flowing, and resumes by
itself if the metrics stack is switched off or its readings go stale. The
point of the exercise is to free the ``root.bg`` channel without ever
creating a window where nobody collects.

The nastiest failure mode is covered here explicitly: when the probe is
skipped it must not write, because every reading would parse as 0 and
overwrite the good values — and a 0% disk reading would then *resolve* a
genuine disk-critical alert.
"""
from unittest.mock import MagicMock

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.host_metrics_executor import (
    HostMetricsExecutor,
)


class HostMetricsRetirementCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.host = self.env["cloud.host"].create({
            "name": "H", "ip_address": "10.0.0.3", "user": "root",
            "wildcard_domain": "h.example.com",
        })

    def _executor(self):
        """Build the executor without __init__ (no SSH, no job record)."""
        ex = object.__new__(HostMetricsExecutor)
        ex.env = self.env
        ex.job = MagicMock(spec=type(self.env["cloud.job"]))
        ex.job.host_id = self.host
        ex._log_buffer = []
        ex._sys = lambda *a, **k: None
        return ex

    def _fresh(self):
        """Mark metrics as flowing for this host.

        Writes ``metrics_last_seen`` and NOT ``last_probed``: the latter
        is written by the SSH job itself, so using it here would test the
        very confusion this suite now guards against.
        """
        self.host.write({"metrics_last_seen": fields.Datetime.now()})


class TestConditionalRetirement(HostMetricsRetirementCase):

    def test_probes_when_observability_is_off(self):
        """With metrics disabled the SSH job behaves exactly as before."""
        self.settings.metrics_enabled = False
        self._fresh()
        self.assertTrue(self._executor().get_commands())

    def test_skips_when_metrics_are_fresh(self):
        """Metrics on and recent → no SSH commands at all."""
        self.settings.metrics_enabled = True
        self._fresh()
        self.assertEqual(self._executor().get_commands(), [])

    def test_probes_again_when_metrics_go_stale(self):
        """A stalled metrics stack must not leave specs uncollected."""
        self.settings.metrics_enabled = True
        stale = fields.Datetime.subtract(fields.Datetime.now(), hours=2)
        self.host.write({"metrics_last_seen": stale})
        self.assertTrue(self._executor().get_commands())

    def test_probes_when_the_host_was_never_seen_by_metrics(self):
        """A host with no metrics reading yet still gets the SSH probe."""
        self.settings.metrics_enabled = True
        self.host.write({"metrics_last_seen": False})
        self.assertTrue(self._executor().get_commands())

    def test_its_own_write_does_not_make_it_stand_down(self):
        """The regression that made the fallback disable itself.

        ``last_probed`` is written by BOTH the metrics reader and this
        SSH job, and the skip decision used to read it. So with
        observability enabled but no agents actually reporting, the job
        ran once, stamped ``last_probed``, then saw its own footprint,
        called metrics "fresh" and skipped — resuming only when the
        freshness window expired. The fallback degraded from every five
        minutes to roughly every fifteen, oscillating, precisely when it
        was the only thing collecting.
        """
        self.settings.metrics_enabled = True
        self.host.write({
            "last_probed": fields.Datetime.now(),
            "metrics_last_seen": False,
        })
        self.assertTrue(
            self._executor().get_commands(),
            "the SSH probe stood down on the strength of its own write",
        )


class TestSkippedRunWritesNothing(HostMetricsRetirementCase):
    """The trap: a skipped probe must not clobber the metrics readings."""

    def setUp(self):
        super().setUp()
        self.settings.metrics_enabled = True
        self._fresh()
        self.host.write({
            "cpu_cores": 8, "ram_total_gb": 16.0,
            "disk_usage": 95.0, "disk_free_gb": 3.0,
        })

    def test_parse_results_is_a_noop_when_skipped(self):
        ex = self._executor()
        ex.get_commands()
        self.assertEqual(ex.parse_results({}), [])

    def test_on_success_does_not_overwrite_with_zeros(self):
        import asyncio

        ex = self._executor()
        ex.get_commands()
        ex.parse_results({})
        asyncio.run(ex.on_success({}))
        self.host.invalidate_recordset()
        self.assertEqual(self.host.cpu_cores, 8)
        self.assertEqual(self.host.ram_total_gb, 16.0)
        self.assertEqual(
            self.host.disk_usage, 95.0,
            "a skipped probe overwrote a real disk reading with 0, which "
            "would also have resolved the disk-critical alert",
        )
