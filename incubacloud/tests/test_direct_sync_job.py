"""``sync_direct_path``: the list in nftables and Traefik, read back, names probed.

The job writes the dynamic document (only when it changed, atomically),
applies the list with ``direct_allowlist.sh``, reads both back and fails
when either is not what it sent. Then it probes the direct names the host
publishes and feeds each outcome to the name's state. It never restarts
Traefik: the file provider reloads on its own.
"""
import asyncio
import shlex
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.full_setup_executor import FullSetupExecutor
from odoo.addons.incubacloud.models.sync_direct_path_executor import (
    _STAGED,
    SyncDirectPathExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.tests._direct import make_host

_GOOD_PROBE = (
    "IC_DIRECT_DNS ok 46.4.0.70\nIC_DIRECT_TLS ok\n"
    "IC_DIRECT_CERT 2027-01-25T10:00:00Z\n"
    "IC_DIRECT_ROUTE POST /w/api/v1/write 401 ok\n"
    "IC_DIRECT_ROUTE GET /flags 403 ok\n"
)


class TestTheSyncJob(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.host = make_host(
            self.env, "sj-central", "46.4.0.70",
            direct_port=9443, direct_port_shipped=9443,
        )
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_central_host_id": self.host.id,
            "metrics_central_direct_host": "direct.metrics.example.test",
        })

    def _executor(self):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "sync_direct_path")],
        )
        job = self.env["cloud.job"].create({
            "host_id": self.host.id, "job_type_id": job_type.id, "name": "sync",
        })
        executor = SyncDirectPathExecutor(job, self.host)
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        asyncio.run(executor.before_execute(transport))
        return executor, transport

    def _results(self, executor, allow=None, readback=None, probe=_GOOD_PROBE):
        """What a run that went well printed; *allow*/*readback* override it."""
        entries = ", ".join(executor._peers) if allow is None else allow
        return {
            "Install the direct list for Traefik": {
                "stdout": "direct.yml updated", "exit_status": 0,
            },
            "Apply the direct list in the firewall": {
                "stdout": f"IC_DIRECT_ALLOW {entries}", "exit_status": 0,
            },
            "Read back the direct list": {
                "stdout": executor._document if readback is None else readback,
                "exit_status": 0,
            },
            "Probe direct.metrics.example.test": {"stdout": probe, "exit_status": 0},
        }

    def test_the_document_is_staged_and_the_list_passed_as_arguments(self):
        executor, transport = self._executor()
        uploaded = transport.upload_text_files.await_args.args[0]
        self.assertEqual(
            uploaded[executor._job_path(_STAGED)], self.host._shipped_direct_yml(),
        )
        commands = {c[0]: c[1] for c in executor.get_commands()}
        apply_ = commands["Apply the direct list in the firewall"]
        self.assertIn("direct_allowlist.sh set 9443 ", apply_)
        self.assertIn("46.4.0.70/32", apply_)
        self.assertIn(
            "direct_probe.sh direct.metrics.example.test 46.4.0.70 9443 "
            "POST:/w/api/v1/write:401 GET:/flags:403",
            commands["Probe direct.metrics.example.test"],
        )

    def test_a_central_registered_by_name_is_not_probed_and_says_so(self):
        """``direct_probe.sh`` takes an IPv4 address: a name would fail the
        step, and with it a job whose list was applied."""
        self.host = make_host(
            self.env, "sj-named", "sj-named.example.com",
            direct_port=9443, direct_port_shipped=9443,
        )
        self.settings.metrics_central_host_id = self.host
        with patch.object(
            SyncDirectPathExecutor, "_sys", autospec=True,
        ) as sys_:
            executor, _transport = self._executor()
        labels = [c[0] for c in executor.get_commands()]
        self.assertFalse([label for label in labels if label.startswith("Probe ")])
        self.assertIn(
            "Apply the direct list in the firewall", labels,
        )
        said = " ".join(c.args[1] for c in sys_.call_args_list)
        self.assertIn("no global IPv4 address", said)

    def test_an_entry_with_shell_in_it_is_one_quoted_argument(self):
        with patch.object(
            type(self.host), "_direct_peers_entries", autospec=True,
            return_value=["10.0.0.0/8", "1.2.3.4;flush ruleset"],
        ):
            executor, _transport = self._executor()
        apply_ = {c[0]: c[1] for c in executor.get_commands()}[
            "Apply the direct list in the firewall"
        ]
        self.assertIn(shlex.quote("1.2.3.4;flush ruleset"), apply_)
        self.assertIn("'1.2.3.4;flush ruleset'", apply_)

    def test_a_list_read_back_merged_is_the_same_list(self):
        executor, _transport = self._executor()
        self.assertEqual(executor.parse_results(self._results(executor)), [])

    def test_a_list_that_is_not_the_one_sent_fails_the_job(self):
        executor, _transport = self._executor()
        errors = executor.parse_results(self._results(executor, allow="10.0.0.0/8"))
        self.assertTrue([e for e in errors if "nftables" in e])
        errors = executor.parse_results(self._results(executor, readback="other"))
        self.assertTrue([e for e in errors if "Traefik" in e])

    def test_the_list_is_recorded_only_by_a_good_run(self):
        executor, _transport = self._executor()
        asyncio.run(executor.on_failure(self._results(executor), ["boom"]))
        self.assertFalse(self.host.direct_allowlist_shipped)
        asyncio.run(executor.on_success(self._results(executor)))
        self.assertEqual(self.host.direct_allowlist_shipped.split(), executor._peers)
        self.assertTrue(self.host.direct_allowlist_synced_at)

    def test_the_log_names_what_entered_and_left_not_the_whole_list(self):
        peers = self.env["cloud.host"]._direct_peers_entries()
        self.host.direct_allowlist_shipped = "\n".join(
            [p for p in peers if p != "46.4.0.70/32"] + ["46.4.0.99/32"],
        )
        executor, _transport = self._executor()
        with patch.object(SyncDirectPathExecutor, "_sys", autospec=True) as sys_:
            asyncio.run(executor.on_success(self._results(executor)))
        log = " ".join(c.args[1] for c in sys_.call_args_list)
        self.assertIn("in: 46.4.0.70/32", log)
        self.assertIn("out: 46.4.0.99/32", log)
        self.assertNotIn("127.0.0.0/8", log)

    def test_the_probe_moves_the_names_state(self):
        executor, _transport = self._executor()
        asyncio.run(executor.on_success(self._results(executor)))
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_central_direct_state, "ok")
        self.assertTrue(self.settings.metrics_central_direct_cert_expires)

    def test_a_missing_tool_fails_and_leaves_the_state(self):
        executor, _transport = self._executor()
        results = self._results(executor, probe="IC_DIRECT_TOOLS missing openssl")
        results["Probe direct.metrics.example.test"]["exit_status"] = 3
        errors = executor.parse_results(results)
        self.assertTrue([e for e in errors if "openssl" in e])

    def test_a_host_without_a_direct_port_is_refused(self):
        self.host.direct_port_shipped = 0
        executor, _transport = self._executor()
        with self.assertRaises(JobRefused):
            executor.pre_run_checks()

    def test_full_setup_queues_it_when_it_publishes_a_port(self):
        job_type = self.env["cloud.job.type"].search([("code", "=", "full_setup")])
        job = self.env["cloud.job"].create({
            "host_id": self.host.id, "job_type_id": job_type.id, "name": "fs",
        })
        with patch.object(
            type(self.host), "_enqueue_observability",
            autospec=True, return_value=False,
        ):
            asyncio.run(FullSetupExecutor(job, self.host).on_success({}))
        self.assertTrue(self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "sync_direct_path"),
        ]))
