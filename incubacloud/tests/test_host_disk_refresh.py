"""The host's disk is read from metrics as soon as something changes it.

The disk the panel shows comes from node_exporter, copied to the host
by the specs cron every five minutes, so after a build, a delete or a
press of Check it could still show the old figure for minutes. Those
jobs now read it again, for their host only, when they end — whatever
their outcome — and nothing they do with it can fail them: no metrics,
an unreachable backend or a failed write leave the host as it was.
"""
import asyncio
from unittest.mock import patch

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models.abstract_executor import AbstractExecutor
from odoo.addons.incubacloud.models.cloud_host_metrics import CloudHost
from odoo.addons.incubacloud.models.delete_instance_executor import (
    DeleteInstanceExecutor,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.host_probe_executor import HostProbeExecutor
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.restart_instance_executor import (
    RestartInstanceExecutor,
)

from .test_host_metrics_handover import _GIB, HostMetricsHandoverCase

_PROMQL = "odoo.addons.incubacloud.models.cloud_host_metrics.promql_query"


class TestOneHostIsReadNow(HostMetricsHandoverCase):

    def setUp(self):
        super().setUp()
        self.other = self.env["cloud.host"].create({
            "name": "HM2",
            "ip_address": "10.0.0.21",
            "user": "root",
            "wildcard_domain": "hm2.example.com",
        })
        self.host.write({"disk_free_gb": 1.0, "disk_usage": 99.0})
        self.other.write({"disk_free_gb": 2.0, "disk_usage": 98.0})

    def _backend(self, free_gb):
        """A metrics backend reporting *free_gb* free of 100 GB on both hosts.

        :param dict free_gb: ``{host: free GB}``
        :return: a ``promql_query`` stand-in
        """
        def answer(_base, expression, **_kwargs):
            if "avail_bytes" in expression:
                return [({"host_id": str(h.id)}, gb * _GIB) for h, gb in free_gb.items()]
            if "size_bytes" in expression:
                return [({"host_id": str(h.id)}, 100 * _GIB) for h in free_gb]
            if "MemTotal" in expression:
                return [({"host_id": str(h.id)}, 8 * _GIB) for h in free_gb]
            return [({"host_id": str(h.id)}, 4.0) for h in free_gb]
        return answer

    def test_only_the_host_asked_for_is_written(self):
        with patch(_PROMQL, autospec=True,
                   side_effect=self._backend({self.host: 60, self.other: 70})):
            self.host._refresh_specs_from_metrics_now()
        self.assertEqual(self.host.disk_free_gb, 60.0)
        self.assertEqual(self.host.disk_usage, 40.0)
        self.assertTrue(self.host.metrics_last_seen)
        self.assertEqual(self.other.disk_free_gb, 2.0)
        self.assertEqual(self.other.disk_usage, 98.0)

    def test_a_host_the_backend_does_not_know_is_left_alone(self):
        with patch(_PROMQL, autospec=True,
                   side_effect=self._backend({self.other: 70})):
            self.host._refresh_specs_from_metrics_now()
        self.assertEqual(self.host.disk_free_gb, 1.0)
        self.assertFalse(self.host.metrics_last_seen)

    def test_an_unreachable_backend_changes_nothing_and_raises_nothing(self):
        with patch(_PROMQL, autospec=True, side_effect=OSError("no route")):
            self.host._refresh_specs_from_metrics_now()
        self.assertEqual(self.host.disk_free_gb, 1.0)

    def test_with_metrics_off_the_backend_is_not_asked(self):
        self.settings.metrics_enabled = False
        with patch(_PROMQL, autospec=True) as promql:
            self.host._refresh_specs_from_metrics_now()
        promql.assert_not_called()

    def test_a_write_that_fails_raises_nothing(self):
        with patch(_PROMQL, autospec=True,
                   side_effect=self._backend({self.host: 60})), \
                patch.object(CloudHost, "_write_specs_from_metrics", autospec=True,
                             side_effect=RuntimeError("serialization")):
            self.host._refresh_specs_from_metrics_now()
        self.assertEqual(self.host.disk_free_gb, 1.0)

    def test_the_cron_still_reads_every_host(self):
        with patch(_PROMQL, autospec=True,
                   side_effect=self._backend({self.host: 60, self.other: 70})) as promql:
            self._cron()
        self.assertEqual(self.host.disk_free_gb, 60.0)
        self.assertEqual(self.other.disk_free_gb, 70.0)
        for call in promql.call_args_list:
            self.assertNotIn("host_id=~", call.args[1])

    def test_the_backend_is_asked_about_that_host_only(self):
        """Every query carries the host's own matcher, beside its own."""
        with patch(_PROMQL, autospec=True,
                   side_effect=self._backend({self.host: 60})) as promql:
            self.host._refresh_specs_from_metrics_now()
        expressions = [call.args[1] for call in promql.call_args_list]
        self.assertEqual(len(expressions), 6)
        for expression in expressions:
            self.assertIn(f'host_id=~"{self.host.id}"', expression)
        self.assertIn(
            f'node_filesystem_avail_bytes{{mountpoint="/",host_id=~"{self.host.id}"}}',
            expressions,
        )
        self.assertIn(
            f'node_memory_MemTotal_bytes{{host_id=~"{self.host.id}"}}', expressions,
        )


class TestWhichJobsReadIt(BaseCase):

    def test_builds_teardowns_and_the_check_read_it(self):
        for cls in (DeployInstanceExecutor, RebuildInstanceExecutor,
                    DeleteInstanceExecutor, HostProbeExecutor):
            with self.subTest(executor=cls.__name__):
                self.assertTrue(cls._refreshes_host_disk)

    def test_other_jobs_do_not(self):
        self.assertFalse(AbstractExecutor._refreshes_host_disk)
        self.assertFalse(RestartInstanceExecutor._refreshes_host_disk)


class TestTheReadComesAfterTheOutcome(HostMetricsHandoverCase):
    """``_dispatch_outcome`` reads the disk once its hook ran, either way."""

    def _executor(self):
        job_type = self.env["cloud.job.type"].search([("code", "=", "host_probe")], limit=1)
        job = self.env["cloud.job"].create({
            "host_id": self.host.id,
            "job_type_id": job_type.id,
            "name": "Host Probe",
        })
        ex = object.__new__(HostProbeExecutor)
        ex.job = job
        ex.env = job.env
        ex._log_buffer = []
        return ex

    def _dispatch(self, errors, hook_raises=False):
        """Run ``_dispatch_outcome`` with *errors*, the hook and the read recorded.

        :return: how many times the disk was read, and what the dispatch raised
        """
        ex = self._executor()
        raised = None
        with patch.object(HostProbeExecutor, "_outcome_errors", autospec=True,
                          return_value=errors), \
                patch.object(HostProbeExecutor, "_run_hook_durably", autospec=True,
                             side_effect=RuntimeError("hook") if hook_raises else None), \
                patch.object(CloudHost, "_refresh_specs_from_metrics_now",
                             autospec=True) as refresh:
            try:
                asyncio.run(ex._dispatch_outcome({}))
            except RuntimeError as exc:
                raised = exc
        return refresh, raised

    def test_a_success_reads_the_host_s_disk(self):
        refresh, raised = self._dispatch([])
        self.assertIsNone(raised)
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0].ids, [self.host.id])

    def test_a_failure_reads_it_too_and_still_fails(self):
        refresh, raised = self._dispatch(["'probe' exited with status 1"])
        self.assertIn("probe", str(raised))
        refresh.assert_called_once()

    def test_a_hook_that_raised_is_not_followed_by_a_read(self):
        refresh, raised = self._dispatch([], hook_raises=True)
        self.assertEqual(str(raised), "hook")
        refresh.assert_not_called()

    def test_a_run_that_aborted_reads_it_too(self):
        """Ended before its outcome: a build stopped half way changed the
        disk as well."""
        ex = self._executor()
        with patch.object(CloudHost, "_refresh_specs_from_metrics_now",
                          autospec=True) as refresh:
            asyncio.run(ex._dispatch_abort(RuntimeError("lost")))
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0].ids, [self.host.id])

    def test_a_read_that_cannot_open_its_cursor_does_not_fail_the_job(self):
        ex = self._executor()
        with patch.object(HostProbeExecutor, "_durable_env", autospec=True,
                          side_effect=RuntimeError("no cursor")):
            ex._refresh_host_disk()
