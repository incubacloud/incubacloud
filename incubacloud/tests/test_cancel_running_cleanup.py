"""A job cancelled while it ran is closed as a failure would close it.

Issue #2 (6-nov-2026): a deploy cancelled while it ran ended
``cancelled`` with neither ``on_failure`` nor ``on_abort`` run, so the instance stayed in
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
    JobRefused,
    JobWorkerLost,
)
from odoo.addons.incubacloud.models.cloud_host import CloudHost
from odoo.addons.incubacloud.models.delete_instance_executor import (
    DeleteInstanceExecutor,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.full_setup_executor import FullSetupExecutor
from odoo.addons.incubacloud.models.host_probe_executor import HostProbeExecutor
from odoo.addons.incubacloud.models.move_rollback_cleanup_executor import (
    MoveRollbackCleanupExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.setup_whitelist_executor import (
    SetupWhitelistExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

from .test_blocked_and_aborted_jobs import _DETECT, _DeployJobCase


async def _hang(*_args, **_kwargs):
    """A step, or a preparation, that is still running when the stop comes."""
    await asyncio.sleep(30)


async def _answer(*_args, **_kwargs):
    """A step that ends at once, and well."""
    return CommandResult(stdout="", exit_status=0)


class TestACancelledRunIsClosed(_DeployJobCase):
    """``run`` hands the stop to the executor's failure path."""

    def _transport(self, answered=0):
        """An open connection whose commands run until they are stopped.

        :param int answered: how many commands end at once, and well,
            before the one that runs until the stop
        """
        transport = MagicMock(spec=SSHTransport)
        calls = []

        async def _execute(*args, **kwargs):
            """End the first *answered* commands at once; hang on the next."""
            calls.append(args)
            if len(calls) <= answered:
                return await _answer()
            return await _hang()

        transport.execute.side_effect = _execute

        @contextlib.asynccontextmanager
        async def _connect(_host):
            yield transport

        return _connect

    def _cancelled_run(
        self, executor_cls=DeployInstanceExecutor, before_steps=False,
        commands=(("Initialize database", "true"),),
    ):
        """Cancel the job, then run *executor_cls* until it sees the flag.

        :param bool before_steps: stop it while it prepares, before the
            first step
        :param commands: the run's steps; all but the last end at once,
            the last runs until the stop
        """
        self.job.cancel_job()
        self.env.flush_all()
        executor = executor_cls(self.job, self.host, sleep_interval=0.01)
        with patch.object(
            CloudHost, "get_transport", autospec=True,
            side_effect=self._transport(answered=len(commands) - 1),
        ), patch.object(
            executor_cls, "before_execute", autospec=True,
            side_effect=_hang if before_steps else None,
        ), patch.object(
            executor_cls, "get_commands", autospec=True,
            return_value=list(commands),
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

    def _cancelled_rebuild(self, commands):
        """A deployed instance whose rebuild is stopped on its last step.

        :return: the mock of the state probe the hook asks for
        """
        self.inst._transition("deployed")
        self.inst.status = "ok"
        self.env.flush_all()
        with patch.object(
            type(self.env["cloud.instance"]), "_refresh_state_now", autospec=True,
        ) as probe:
            self._cancelled_run(RebuildInstanceExecutor, commands=commands)
        return probe

    def test_a_rebuild_cancelled_halfway_leaves_the_status_alone(self):
        """Stopped before copier, it touched nothing the instance runs on:
        it is not marked in error (6-nov-2026), and its state is read
        again."""
        probe = self._cancelled_rebuild([("Commit dirty files", "true")])
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "ok")
        probe.assert_called_once()
        self.assertIn("status is unchanged", self._log())
        self.assertNotIn("rewrote the instance's files", self._log())

    def test_a_rebuild_stopped_after_copier_says_what_it_left(self):
        """copier rewrote the stack's files and nothing restarted it."""
        self._cancelled_rebuild([
            ("Update with copier", "true"), ("Rebuild Odoo image", "true"),
        ])
        self.assertEqual(self.inst.status, "ok")
        self.assertIn("may have rebuilt its image", self._log())

    def test_a_rebuild_stopped_after_its_restart_says_nothing_of_it(self):
        self._cancelled_rebuild([
            ("Update with copier", "true"), ("Restart instance", "true"),
            ("Set system parameters", "true"),
        ])
        self.assertNotIn("rewrote the instance's files", self._log())

    def _rebuild_closed_by(self, dispatch, exc):
        """Close a rebuild whose earlier attempt logged copier, no results.

        :param str dispatch: ``_dispatch_cancel`` or ``_dispatch_abort``
        :param Exception exc: what it is closed with
        """
        self.inst._transition("deployed")
        self.inst.status = "ok"
        self.env["cloud.job.log.chunk"].create([
            {"job_id": self.job.id, "source": "system", "content": line}
            for line in ("Running: Commit dirty files", "Running: Update with copier")
        ])
        self.env.flush_all()
        executor = RebuildInstanceExecutor(self.job, self.host)
        with patch.object(
            type(self.env["cloud.instance"]), "_refresh_state_now", autospec=True,
        ), patch.object(
            RebuildInstanceExecutor, "_refresh_host_disk", autospec=True,
        ), patch.object(RebuildInstanceExecutor, "_publish_bus", autospec=True):
            asyncio.run(getattr(executor, dispatch)(exc))
            executor._flush_logs()
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.status, "ok")
        self.assertIn("rewrote the instance's files", self._log())
        self.assertNotIn("may have rebuilt its image", self._log())

    def test_a_rebuild_ended_in_its_update_says_the_database_moved_on(self):
        """A worker that died in «Update changed modules»: Odoo stores each
        module as it goes, so the database may be ahead of the image that
        runs, and the warning no longer says it was not applied."""
        self.inst._transition("deployed")
        self.inst.status = "ok"
        self.env["cloud.job.log.chunk"].create([
            {"job_id": self.job.id, "source": "system", "content": line}
            for line in (
                "Running: Update with copier", "Running: Rebuild Odoo image",
                "Running: Update changed modules",
            )
        ])
        self.env.flush_all()
        executor = RebuildInstanceExecutor(self.job, self.host)
        with patch.object(
            RebuildInstanceExecutor, "_refresh_host_disk", autospec=True,
        ), patch.object(RebuildInstanceExecutor, "_publish_bus", autospec=True):
            asyncio.run(executor._dispatch_abort(JobWorkerLost("lost")))
            executor._flush_logs()
        self.assertIn("updated the modules of the instance's database", self._log())
        self.assertNotIn("neither boot-tested nor applied", self._log())

    def test_a_rebuild_handed_no_results_reads_its_log(self):
        """A stop whose worker died: the steps an earlier attempt logged."""
        self._rebuild_closed_by("_dispatch_cancel", JobCancelled("stop"))

    def test_a_rebuild_whose_worker_was_lost_says_what_it_left(self):
        """Closed by the reconciliation: ``on_abort``, with the same word."""
        self._rebuild_closed_by("_dispatch_abort", JobWorkerLost("lost"))

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


class TestAStopThatCameTooLate(_DeployJobCase):
    """What ended, or began to close, before the stop was read is not stopped."""

    @contextlib.contextmanager
    def _finishing_run(self, on_success=None, before_execute=None):
        """Run the deploy's one step at once and well, its ``on_success`` mocked.

        :param on_success: what the mocked ``on_success`` does, if anything
        :param before_execute: what the mocked ``before_execute`` does
        :return: the mocked ``on_success`` and ``on_failure``
        """
        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = _answer

        @contextlib.asynccontextmanager
        async def _connect(_host):
            yield transport

        with patch(_DETECT, autospec=True, return_value=[]), patch.object(
            DeployInstanceExecutor, "pre_run_checks", autospec=True,
        ), patch.object(
            CloudHost, "get_transport", autospec=True, side_effect=_connect,
        ), patch.object(
            DeployInstanceExecutor, "before_execute", autospec=True,
            side_effect=before_execute,
        ), patch.object(
            DeployInstanceExecutor, "get_commands", autospec=True,
            return_value=[("Initialize database", "true")],
        ), patch.object(
            DeployInstanceExecutor, "_refresh_host_disk", autospec=True,
        ), patch.object(
            DeployInstanceExecutor, "on_success", autospec=True,
            side_effect=on_success,
        ) as success, patch.object(
            DeployInstanceExecutor, "on_failure", autospec=True,
        ) as failure:
            yield success, failure

    def test_a_run_that_ended_as_the_flag_was_read_stays_done(self):
        """The loop read the flag on the tick the run ended in: a cutover
        that had cut over ended cancelled, and the freeze of the source
        after it with it, so the old copy went on running. The stop comes
        while the run prepares: one before ``execute`` is a stop before
        the attempt began, which does not run (6-nov-2026)."""
        head = self.job
        head.queue_job_id.graph_uuid = "graph-ended-well"
        tail = self._deploy_job("uuid-after")
        tail.queue_job_id.write({
            "state": "wait_dependencies", "graph_uuid": "graph-ended-well",
        })
        self.env["cloud.job"].invalidate_model(["state"])
        self.env.flush_all()

        def _stop_now(*_args):
            """A person cancels the job while it prepares."""
            head.cancel_job()
            head.env.flush_all()

        with self._finishing_run(before_execute=_stop_now) as (success, failure):
            result = head.execute()
        self.assertIsNone(result)
        success.assert_awaited_once()
        failure.assert_not_awaited()
        head.queue_job_id.write({"state": "done", "result": result})
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(head.queue_job_id.state, "done")
        self.assertEqual(head.state, "done")
        self.assertEqual(tail.state, "wait_dependencies")

    def test_a_stop_during_the_success_hook_leaves_the_success(self):
        """A hook that awaits — a restore's, a hardening's — was cut off
        by the stop: what it wrote was undone, and ``on_failure`` then ran
        after steps that had all passed."""
        reached = []

        async def _slow_success(executor, results):
            executor.job.cancel_job()
            executor.env.flush_all()
            await asyncio.sleep(0.2)
            reached.append(results)

        executor = DeployInstanceExecutor(self.job, self.host, sleep_interval=0.01)
        with self._finishing_run(_slow_success) as (_success, failure):
            executor.run()
        self.assertEqual(
            reached, [{"Initialize database": {"stdout": "", "exit_status": 0}}],
        )
        failure.assert_not_awaited()

    def test_a_stop_during_on_abort_lets_it_finish(self):
        """A run that failed before its steps is closing: a stop read
        while ``on_abort`` awaited cut it off, undid what it wrote, and
        ran it again as the cancellation's, with no alert."""
        reached = []

        async def _slow_abort(executor, exc):
            if not reached:
                executor.job.cancel_job()
                executor.env.flush_all()
            await asyncio.sleep(0.2)
            reached.append(exc)

        executor = DeployInstanceExecutor(self.job, self.host, sleep_interval=0.01)
        with self._finishing_run(
            before_execute=RuntimeError("the host refused the files"),
        ), patch.object(
            DeployInstanceExecutor, "on_abort", autospec=True,
            side_effect=_slow_abort,
        ) as on_abort:
            # Not ``assertRaises``: it rolls back what the block wrote.
            try:
                executor.run()
            except RuntimeError as exc:
                raised = exc
            else:
                self.fail("the run did not fail")
        self.assertEqual(str(raised), "the host refused the files")
        on_abort.assert_awaited_once()
        self.assertEqual(reached, [raised])


class TestATeardownCancelledBeforeItsSteps(_DeployJobCase):
    """A job that moved its record before its first step puts it back, one
    that ended before moving it leaves it as it was, and one that does not
    own the record leaves it alone."""

    def _cancel_before_steps(self, executor, results=None):
        """Close *executor*'s run as a stop before its first step closes it.

        :param results: what the steps answered, for a stop halfway
        """
        executor._step_results = results
        with patch.object(type(executor), "_refresh_host_disk", autospec=True):
            asyncio.run(executor._dispatch_cancel(JobCancelled(CANCELLED_WHILE_RUNNING)))

    def _job_of(self, code):
        """A job of type *code* for the instance, on its host."""
        return self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            ).id,
            "name": code,
        })

    def test_a_delete_goes_back_to_deployed(self):
        """In 'deleting' for good: neither deployed again nor deleted."""
        self.inst._transition("deployed")
        self.inst._transition("deleting")
        self.env.flush_all()
        self._cancel_before_steps(
            DeleteInstanceExecutor(self._job_of("delete_instance"), self.host),
        )
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "error")

    def test_a_rollback_cleanup_leaves_the_live_instance_alone(self):
        """It inherits the delete's ``on_abort``, and tears down a copy of
        an instance that lives on elsewhere: nothing of it is marked."""
        self.inst._transition("deployed")
        self.inst.status = "ok"
        self.env.flush_all()
        self._cancel_before_steps(MoveRollbackCleanupExecutor(
            self._job_of("move_rollback_cleanup"), self.host,
        ))
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "ok")

    def test_a_rollback_cleanup_cancelled_halfway_leaves_the_live_instance_alone(self):
        """Stopped during its teardown, it closes through the delete's
        ``on_failure``, which marked the instance on the source, live, in
        error: the record is not this teardown's."""
        self.inst._transition("deployed")
        self.inst.status = "ok"
        self.env.flush_all()
        self._cancel_before_steps(MoveRollbackCleanupExecutor(
            self._job_of("move_rollback_cleanup"), self.host,
        ), results={"Stop containers": {"stdout": "", "exit_status": 0}})
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "ok")

    def test_a_setup_does_not_leave_its_server_checking(self):
        """'checking' for good; no alert of its failure, as it was stopped."""
        self.host.status = "checking"
        self.env.flush_all()
        self._cancel_before_steps(
            FullSetupExecutor(self._job_of("full_setup"), self.host),
        )
        self.host.invalidate_recordset()
        self.assertEqual(self.host.status, "unsupported")
        self.assertFalse(self.env["cloud.alert"].search([
            ("code", "=", "setup_failed"), ("host_id", "=", self.host.id),
        ]))

    # A delete or a setup that ended before ``before_execute`` — refused,
    # no connection, cancelled while it connected — leaves its record as
    # it was: only the state ``before_execute`` sets is put back.

    def _abort_before_steps(self, executor, exc):
        """Close *executor*'s run as one that ended before its steps.

        :param Exception exc: what ended it
        """
        with patch.object(type(executor), "_refresh_host_disk", autospec=True):
            asyncio.run(executor._dispatch_abort(exc))
        # As ``run`` writes the log once the run ended.
        executor._flush_logs()

    def _setup_alerts(self):
        """The ``setup_failed`` alerts of the host."""
        return self.env["cloud.alert"].search([
            ("code", "=", "setup_failed"), ("host_id", "=", self.host.id),
        ])

    def test_a_setup_refused_on_a_working_server_leaves_it_working(self):
        """Marked unsupported, it was no destination of moves or
        placements any more, and a critical alert said the setup failed."""
        self.host.status = "compatible"
        self.env.flush_all()
        job = self._job_of("full_setup")
        self._abort_before_steps(
            FullSetupExecutor(job, self.host),
            JobRefused("The proxy's compose file has no 443 line."),
        )
        self.host.invalidate_recordset()
        self.assertEqual(self.host.status, "compatible")
        self.assertFalse(self._setup_alerts())
        self.assertIn("443 line", "\n".join(
            self.env["cloud.job.log.chunk"]
            .search([("job_id", "=", job.id)])
            .mapped("content")
        ))

    def test_a_setup_cancelled_while_it_connected_leaves_the_server_working(self):
        """Stopped before ``before_execute``: nothing of the server changed."""
        self.host.status = "compatible"
        self.env.flush_all()
        self._cancel_before_steps(
            FullSetupExecutor(self._job_of("full_setup"), self.host),
        )
        self.host.invalidate_recordset()
        self.assertEqual(self.host.status, "compatible")
        self.assertFalse(self._setup_alerts())

    def test_a_setup_that_lost_its_server_once_checking_still_fails(self):
        """After ``before_execute``: unsupported, with the alert."""
        self.host.status = "checking"
        self.env.flush_all()
        self._abort_before_steps(
            FullSetupExecutor(self._job_of("full_setup"), self.host),
            ConnectionError("the upload of the proxy's files dropped"),
        )
        self.host.invalidate_recordset()
        self.assertEqual(self.host.status, "unsupported")
        self.assertTrue(self._setup_alerts())

    def test_a_delete_that_could_not_connect_leaves_the_instance_live(self):
        """A deployed instance was marked in error by a delete that never
        reached its host."""
        self.inst._transition("deployed")
        self.inst.status = "ok"
        self.env.flush_all()
        job = self._job_of("delete_instance")
        self._abort_before_steps(
            DeleteInstanceExecutor(job, self.host),
            ConnectionError("the host did not answer"),
        )
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "ok")
        self.assertIn("the host did not answer", "\n".join(
            self.env["cloud.job.log.chunk"]
            .search([("job_id", "=", job.id)])
            .mapped("content")
        ))

    def test_a_delete_that_lost_its_host_once_deleting_goes_back(self):
        """After ``before_execute``: back to deployed, in error."""
        self.inst._transition("deployed")
        self.inst._transition("deleting")
        self.env.flush_all()
        self._abort_before_steps(
            DeleteInstanceExecutor(self._job_of("delete_instance"), self.host),
            ConnectionError("the connection dropped"),
        )
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "error")

    def test_a_delete_whose_steps_failed_still_marks_the_instance_in_error(self):
        """The teardown ran and failed: the instance is still on the host."""
        self.inst._transition("deployed")
        self.inst._transition("deleting")
        self.inst.status = "ok"
        self.env.flush_all()
        executor = DeleteInstanceExecutor(self._job_of("delete_instance"), self.host)
        with patch.object(
            DeleteInstanceExecutor, "_refresh_host_disk", autospec=True,
        ):
            # Not ``assertRaises``: it rolls back what the block wrote.
            try:
                asyncio.run(executor._dispatch_outcome(
                    {"Stop containers": {"stdout": "", "exit_status": 1}},
                ))
            except RuntimeError:
                pass
            else:
                self.fail("the failed teardown was not a failure")
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "error")
