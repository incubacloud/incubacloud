"""A job cancelled while it ran is closed as a failure would close it.

Issue #2 (6-nov-2026): a deploy cancelled while it ran — its Cancel
button, or the panel restarting under it — ended ``cancelled`` with
neither ``on_failure`` nor ``on_abort`` run, so the instance stayed in
'deploying': every deploy after it was refused ("Cannot move instance
… from 'deploying' to 'deploying'") and so was its delete. The stop now
runs the executor's own failure path (``on_cancelled_running``), raises
no alert, and the job still ends ``cancelled``.
"""
import asyncio
import contextlib
from unittest.mock import MagicMock, patch

from odoo.addons.incubacloud.models.abstract_executor import (
    CANCELLED_WHILE_RUNNING,
    JobCancelled,
)
from odoo.addons.incubacloud.models.cloud_host import CloudHost
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.host_probe_executor import HostProbeExecutor
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.setup_whitelist_executor import (
    SetupWhitelistExecutor,
)
from odoo.addons.incubacloud.models.transport import SSHTransport

from .test_blocked_and_aborted_jobs import _DETECT, _DeployJobCase


async def _hang(*_args, **_kwargs):
    """A step, or a preparation, that is still running when the stop comes."""
    await asyncio.sleep(30)


class TestACancelledRunIsClosed(_DeployJobCase):
    """``run`` hands the stop to the executor's failure path."""

    def _transport(self):
        """An open connection whose commands run until they are stopped."""
        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = _hang

        @contextlib.asynccontextmanager
        async def _connect(_host):
            yield transport

        return _connect

    def _cancelled_run(self, executor_cls=DeployInstanceExecutor, before_steps=False):
        """Cancel the job, then run *executor_cls* until it sees the flag.

        :param bool before_steps: stop it while it prepares, before the
            first step
        """
        self.job.cancel_job()
        self.env.flush_all()
        executor = executor_cls(self.job, self.host, sleep_interval=0.01)
        with patch.object(
            CloudHost, "get_transport", autospec=True, side_effect=self._transport(),
        ), patch.object(
            executor_cls, "before_execute", autospec=True,
            side_effect=_hang if before_steps else None,
        ), patch.object(
            executor_cls, "get_commands", autospec=True,
            return_value=[("Initialize database", "true")],
        ), patch.object(
            executor_cls, "_refresh_host_disk", autospec=True,
        ):
            # Not ``assertRaises``: it rolls back what the block wrote.
            try:
                executor.run()
            except JobCancelled:
                pass
            else:
                self.fail("the run did not stop on the flag")
        self.inst.invalidate_recordset()
        return executor

    def _log(self):
        """The job's log, as one text."""
        return "\n".join(
            self.env["cloud.job.log.chunk"]
            .search([("job_id", "=", self.job.id)])
            .mapped("content")
        )

    def test_a_deploy_cancelled_halfway_goes_back_to_draft(self):
        """Deployable again, and deletable; the log says it was cancelled."""
        self._cancelled_run()
        self.assertEqual(self.inst.state, "draft")
        self.assertEqual(self.inst.status, "error")
        self.assertIn(CANCELLED_WHILE_RUNNING, self._log())
        self.inst._transition("deploying")

    def test_a_deploy_cancelled_before_its_steps_goes_back_to_draft(self):
        """Stopped while it prepared: ``on_abort``, the deploy's failure."""
        self._cancelled_run(before_steps=True)
        self.assertEqual(self.inst.state, "draft")
        self.assertIn(CANCELLED_WHILE_RUNNING, self._log())

    def test_a_rebuild_cancelled_halfway_is_closed_as_its_failure(self):
        """It leaves its run with ``on_failure``: the instance stays
        deployed, marked in error until the next rebuild or probe."""
        self.inst._transition("deployed")
        self.inst.status = "ok"
        self.env.flush_all()
        self._cancelled_run(RebuildInstanceExecutor)
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "error")

    def test_a_cancelled_run_raises_no_alert(self):
        """What its failure path would raise — here the addon conflict the
        log shows — is not raised for a stop a person asked for."""
        self.env["cloud.job.log.chunk"].create({
            "job_id": self.job.id,
            "source": "stderr",
            "content": "AddonsConfigError: Addon web_sheet_full_width defined "
                       "in several repos {'odoo-addons', 'web'}",
        })
        self._cancelled_run()
        self.assertEqual(self.inst.state, "draft")
        self.assertFalse(self.env["cloud.alert"].search([
            "|", ("job_id", "=", self.job.id), ("instance_id", "=", self.inst.id),
        ]))

    def test_the_job_ends_cancelled_and_the_instance_in_draft(self):
        """Through ``execute`` and what queue_job stores: cancelled, no
        failure alert, and the instance ready to deploy again."""
        self.job.cancel_job()
        self.env.flush_all()
        with patch(_DETECT, autospec=True, return_value=[]), patch.object(
            DeployInstanceExecutor, "pre_run_checks", autospec=True,
        ), patch.object(
            CloudHost, "get_transport", autospec=True, side_effect=self._transport(),
        ), patch.object(
            DeployInstanceExecutor, "before_execute", autospec=True,
        ), patch.object(
            DeployInstanceExecutor, "get_commands", autospec=True,
            return_value=[("Initialize database", "true")],
        ), patch.object(
            DeployInstanceExecutor, "_refresh_host_disk", autospec=True,
        ):
            result = self.job.execute()
        self.assertEqual(result, CANCELLED_WHILE_RUNNING)
        qjob = self.job.queue_job_id
        qjob.write({"state": "done", "result": result})
        self.env["cloud.job"].invalidate_model(["state"])
        self.inst.invalidate_recordset()
        self.assertEqual(qjob.state, "cancelled")
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(self.inst.state, "draft")
        self.assertFalse(self.env["cloud.alert"].search([
            ("job_id", "=", self.job.id), ("code", "=", "job_failed"),
        ]))


class TestACancelledRunsOwnPath(_DeployJobCase):
    """Executors whose failure path does not fit a stop."""

    def test_a_cancelled_probe_leaves_the_host_as_it_was(self):
        """``on_failure`` marks the host unsupported for what the probe
        found; a probe stopped halfway found nothing."""
        self.host.status = "compatible"
        executor = HostProbeExecutor(self.job, self.host)
        executor._step_results = {}
        with patch.object(HostProbeExecutor, "_refresh_host_disk", autospec=True):
            asyncio.run(executor._dispatch_cancel(JobCancelled()))
        self.host.invalidate_recordset()
        self.assertEqual(self.host.status, "compatible")

    def test_a_cancelled_setup_raises_no_alert_of_its_failure(self):
        """The whitelist's failure alert is for a setup that failed."""
        executor = SetupWhitelistExecutor(self.job, self.host)
        executor._step_results = {}
        asyncio.run(executor._dispatch_cancel(JobCancelled()))
        self.assertFalse(self.env["cloud.alert"].search([
            ("code", "=", "whitelist_setup_failed"), ("host_id", "=", self.host.id),
        ]))
        executor = SetupWhitelistExecutor(self.job, self.host)
        asyncio.run(executor.on_failure({}, ["'whitelist' exited with status 2"]))
        self.assertTrue(self.env["cloud.alert"].search([
            ("code", "=", "whitelist_setup_failed"), ("host_id", "=", self.host.id),
        ]))


class TestADeployThatNeverRan(_DeployJobCase):
    """A deploy dispatched but never started leaves 'deploying' too."""

    def test_a_deploy_cancelled_once_dispatched_goes_back_to_draft(self):
        """Cancel pressed before the worker picked it up: queue_job skips
        it (``button_cancelled``) and nothing reached the host."""
        self.job.queue_job_id.write({"state": "pending"})
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(self.job.state, "pending")
        # What ``cancel_job`` has ``button_cancelled`` store.
        self.job.queue_job_id.write({"state": "cancelled"})
        self.env["cloud.job"].invalidate_model(["state"])
        self.inst.invalidate_recordset()
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(self.inst.state, "draft")

    def test_a_deploy_its_chain_ended_goes_back_to_draft(self):
        """The step before it was cancelled while it ran: the deploy that
        waited on it never runs (``_end_chain_after``)."""
        head = self.job
        head.queue_job_id.graph_uuid = "graph-cancel-deploy"
        tail = self._deploy_job("uuid-tail")
        tail.queue_job_id.write({
            "state": "wait_dependencies", "graph_uuid": "graph-cancel-deploy",
        })
        self.env["cloud.job"].invalidate_model(["state"])
        head.cancel_job()
        self.env.flush_all()
        head.queue_job_id.write({"state": "done", "result": CANCELLED_WHILE_RUNNING})
        self.env["cloud.job"].invalidate_model(["state"])
        self.inst.invalidate_recordset()
        self.assertEqual(tail.state, "cancelled")
        self.assertEqual(self.inst.state, "draft")
