"""Full Setup installs a server's tools apart from the request that runs it.

«Install host tools» installs Docker and the packages of a new server:
seconds on a server set up before, minutes on a new one (106 s measured,
more with a slow mirror). On Odoo.sh a request is cut at 900 s, so the
step runs apart on the server (``detached_step.sh``) and the job parks
around it, as the rebuild's build does (phase 3, cut 6, 12-nov-2026). The
other steps are short and stay attached, with the options they had.

A server that lacks a tool the script needs (79) runs the step attached,
as before, and the job log says why. What the closing hooks read that
``before_execute`` set (the certificates retired from the store) is kept
for an attempt that takes the job up after a park.
"""
import asyncio
import json

from unittest.mock import AsyncMock, MagicMock

from odoo.tests.common import TransactionCase

from ..models.abstract_executor import DETACHED_EXIT_MISSING_TOOLS
from ..models.full_setup_executor import FullSetupExecutor
from ..models.transport import CommandResult, SSHTransport

_INSTALL = "Install host tools"


class _FullSetupCase(TransactionCase):
    """A host and a Full Setup job of it, dispatched to queue_job."""

    def setUp(self):
        """The host, the job and its queue job."""
        super().setUp()
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        self.host = self.env["cloud.host"].create({
            "name": "fs-apart-host",
            "ip_address": "192.0.2.93",
            "user": "ubuntu",
            "wildcard_domain": "fs-apart.example.com",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "full_setup")], limit=1,
        )
        qjob = self.env["queue.job"].sudo().create({
            "uuid": "fs-apart-qj",
            "name": "full setup",
            "state": "started",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        self.job = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": job_type.id,
            "name": "Full Setup",
            "queue_job_uuid": qjob.uuid,
            "queue_job_id": qjob.id,
        })
        self.env.flush_all()

    def _executor(self):
        """Return core's Full Setup executor of the job."""
        return FullSetupExecutor(self.job, self.host, sleep_interval=0.05)

    def _log(self):
        """The job's log, as one text."""
        return "\n".join(self.env["cloud.job.log.chunk"].search(
            [("job_id", "=", self.job.id)], order="id",
        ).mapped("content"))


class TestFullSetupInstallsItsToolsApart(_FullSetupCase):
    """Which step runs apart, with which options."""

    def test_only_the_install_runs_apart(self):
        """«Install host tools» marked, with no cap of its own (3600 s as
        attached); every other step keeps the options it had."""
        options = {
            item[0]: (item[2] if len(item) == 3 else {})
            for item in self._executor().get_commands()
        }
        self.assertEqual(options.pop(_INSTALL), {"detached": True})
        self.assertFalse([
            label for label, opts in options.items() if opts.get("detached")
        ])

    def test_a_server_without_a_tool_runs_it_attached_and_says_why(self):
        """79: the install runs in the request, as before, and the log
        names the tools the server lacks."""
        executor = self._executor()
        plan = executor.get_commands()
        install = dict(item[:2] for item in plan)[_INSTALL]

        async def _run(command, timeout=3600):
            """The server's answers: 79 to the start, nothing else."""
            if "detached_step.sh start" in command:
                return CommandResult(
                    "[incubacloud] ERROR: this server lacks pgrep pkill:"
                    " the step cannot run apart",
                    DETACHED_EXIT_MISSING_TOOLS,
                )
            return CommandResult("", 0)

        transport = MagicMock(spec=SSHTransport)
        transport.run.side_effect = _run
        transport.execute = AsyncMock(return_value=CommandResult("ok", 0))
        transport.upload_text_files = AsyncMock()

        async def _go():
            """The step loop, then the staging removed as a run removes it."""
            try:
                return await executor._run_steps(transport, plan)
            finally:
                await executor._discard_staging(transport)

        results, _skipped = asyncio.run(_go())
        self.assertEqual(results[_INSTALL], {"stdout": "ok", "exit_status": 0})
        self.assertIn(
            install, [call.args[0] for call in transport.execute.await_args_list],
        )
        self.assertTrue(executor._detached_attached)
        executor._flush_logs()
        log = self._log()
        self.assertIn("lacks pgrep pkill", log)
        self.assertIn(f"'{_INSTALL}' runs attached", log)


class TestFullSetupResumed(_FullSetupCase):
    """An attempt that takes the job up after a park reads what the first set."""

    def test_the_certificates_retired_are_carried(self):
        """``parse_results`` checks the store read back for the names the
        first attempt retired, which only its ``before_execute`` knew."""
        first = self._executor()
        first._acme_retired = ("old.fs-apart.example.com",)
        row = self.env["cloud.job.checkpoint"].sudo().create({
            "job_id": self.job.id,
            "queue_job_uuid": self.job.queue_job_uuid,
            "host_id": self.host.id,
            "next_index": 3,
            "parked": True,
            "data": json.dumps({"plan": [], "carried": first._carried_state()}),
        })
        again = self._executor()
        again._adopt_checkpoint(row)
        self.assertEqual(list(again._acme_retired), ["old.fs-apart.example.com"])
        self.assertTrue(again._acme_prune_errors({}))
