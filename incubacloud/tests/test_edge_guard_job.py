"""``push_edge_guard``: the guard written to a host that is already set up.

One step runs ``edge_guard.sh``. It exits 3 when the proxy does not mount
the dynamic directory and 4 when the handshake was refused and the answer
could not be verified: the file is written either way, so neither fails the
job. 3 raises a warning on the host until a run finds the directory mounted;
4 is only said in the log. 5 (a real HTTP answer other than 403) fails it.
What the proxy reads is recorded on the host, as ``direct_allowlist_shipped``.
"""
import asyncio
from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.cloud_host_edge_guard import (
    EDGE_GUARD_STAGED,
    CloudHostEdgeGuard,
    edge_guard_digest,
)
from odoo.addons.incubacloud.models.push_edge_guard_executor import (
    EDGE_GUARD_ALERT,
    PushEdgeGuardExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.tests._direct import make_host

_INSTALL = "Install the edge guard"


class _EdgeGuardCase(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.host = make_host(self.env, "eg-host", "46.4.0.60", traefik_deployed=True)

    def _executor(self, host=None):
        """Build the job's executor over *host* (the test's by default)."""
        host = host or self.host
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "push_edge_guard")], limit=1,
        )
        job = self.env["cloud.job"].create({
            "host_id": host.id, "job_type_id": job_type.id, "name": "Edge guard",
        })
        return PushEdgeGuardExecutor(job, host)

    def _staged(self, executor):
        """Run ``before_execute``; return what it uploaded."""
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        asyncio.run(executor.before_execute(transport))
        uploaded = {}
        for call in transport.upload_text_files.await_args_list:
            uploaded.update(call.args[0])
        return uploaded

    def _alerts(self):
        return self.env["cloud.alert"].search([
            ("host_id", "=", self.host.id), ("code", "=", EDGE_GUARD_ALERT),
            ("state", "=", "active"),
        ])


class TestTheJobsShape(_EdgeGuardCase):

    def test_the_type_is_a_host_job_that_collapses_and_holds_nothing(self):
        job_type = self.env.ref("incubacloud.push_edge_guard")
        self.assertEqual(job_type.code, "push_edge_guard")
        self.assertEqual(job_type.apply_to, "host")
        self.assertTrue(job_type.queue_collapse)
        self.assertFalse(job_type.failure_holds_queue)
        self.assertFalse(job_type.show_as_action)

    def test_it_stages_the_guard_and_runs_the_script_on_it(self):
        executor = self._executor()
        uploaded = self._staged(executor)
        staged = executor._job_path(EDGE_GUARD_STAGED)
        self.assertEqual(uploaded, {staged: self.host._shipped_edge_guard_yml()})
        commands = executor.get_commands()
        self.assertEqual(len(commands), 1)
        label, command, opts = commands[0]
        self.assertEqual(label, _INSTALL)
        self.assertIn(f"edge_guard.sh install {staged}", command)
        self.assertNotIn("--no-probe", command)
        self.assertNotIn("--no-mount-check", command)
        self.assertEqual(opts, {"continue_on": (3, 4)})


class TestWhatTheScriptSays(_EdgeGuardCase):

    def _run_with(self, status):
        executor = self._executor()
        self._staged(executor)
        results = {_INSTALL: {"stdout": "", "exit_status": status}}
        return executor, results, executor.parse_results(results)

    def test_written_mounted_or_unverified_is_no_error(self):
        for status in (0, 3, 4):
            with self.subTest(status=status):
                self.assertEqual(self._run_with(status)[2], [])

    def test_anything_else_is_an_error(self):
        for status in (1, 2, 127):
            with self.subTest(status=status):
                errors = self._run_with(status)[2]
                self.assertEqual(len(errors), 1)
                self.assertIn(_INSTALL, errors[0])

    def test_a_real_answer_other_than_403_says_what_it_was(self):
        executor = self._executor()
        self._staged(executor)
        results = {_INSTALL: {
            "stdout": "ic_guard.yml updated\nIC_EDGE_GUARD_NOT_APPLIED 200\n",
            "exit_status": 5,
        }}
        errors = executor.parse_results(results)
        self.assertEqual(len(errors), 1)
        self.assertIn("200", errors[0])
        self.assertIn("not applied", errors[0])

    def test_a_step_that_never_ran_is_an_error(self):
        self.assertTrue(self._executor().parse_results({}))

    def test_a_guard_the_proxy_reads_is_recorded_and_clears_the_alert(self):
        self.env["cloud.alert"].raise_alert(
            EDGE_GUARD_ALERT, "not mounted", host=self.host,
        )
        for status in (0, 4):
            with self.subTest(status=status):
                self.host.write({
                    "edge_guard_shipped_hash": False, "edge_guard_shipped_at": False,
                })
                executor, results, _errors = self._run_with(status)
                asyncio.run(executor.on_success(results))
                self.assertEqual(
                    self.host.edge_guard_shipped_hash,
                    edge_guard_digest(self.host._shipped_edge_guard_yml()),
                )
                self.assertTrue(self.host.edge_guard_shipped_at)
                self.assertFalse(self._alerts())

    def test_a_proxy_that_does_not_read_it_raises_a_warning(self):
        executor, results, _errors = self._run_with(3)
        asyncio.run(executor.on_success(results))
        alert = self._alerts()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")
        self.assertIn("Full Setup", alert.message)
        self.assertFalse(self.host.edge_guard_shipped_hash)


class TestWhenTheJobRefuses(_EdgeGuardCase):

    def test_a_host_without_the_panels_proxy(self):
        self.host.traefik_deployed = False
        with self.assertRaisesRegex(JobRefused, "Full Setup"):
            self._executor().pre_run_checks()

    def test_a_host_the_guard_leaves_alone(self):
        original = CloudHostEdgeGuard._edge_guard_leaves_alone
        host_id = self.host.id

        def leaves_alone(host):
            return host.id == host_id or original(host)

        self.patch(type(self.host), "_edge_guard_leaves_alone", leaves_alone)
        with self.assertRaisesRegex(JobRefused, "not written to this host"):
            self._executor().pre_run_checks()

    def test_a_host_set_up_by_the_panel_goes_ahead(self):
        self._executor().pre_run_checks()


class TestWhoGetsIt(_EdgeGuardCase):

    def test_active_hosts_with_the_panels_proxy(self):
        archived = make_host(self.env, "eg-old", "46.4.0.61", traefik_deployed=True)
        archived.active = False
        bare = make_host(self.env, "eg-bare", "46.4.0.62")
        targets = self.env["cloud.host"]._edge_guard_targets()
        self.assertIn(self.host, targets)
        self.assertNotIn(archived, targets)
        self.assertNotIn(bare, targets)
        self.assertTrue(self.host._edge_guard_wanted())
        self.assertFalse(bare._edge_guard_wanted())

    def test_queueing_it_never_fails_the_caller(self):
        def refuse(*_args, **_kwargs):
            raise RuntimeError("queue down")

        self.patch(type(self.env["cloud.job"]), "enqueue", refuse)
        self.assertFalse(self.host._enqueue_edge_guard("test"))
        # The caller's transaction is still usable.
        self.host.name = "eg-host-renamed"
        self.env.flush_all()
