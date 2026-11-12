"""What a job closed with its step still on the server leaves to its queue.

A job can be closed while a step it ran apart still runs on its server:
its worker died for good, its queue.job was ended by hand, a stop came
back to it. A step past the job's point of no return is not stopped
then: it is left to end (``_close_on_server``). Its queue must not start
the next job beside it, so the checkpoint that step leaves keeps the
queue busy (``holds_queue_since``) until the reconciliation finds the
step ended, or until it expires. A step before the point of no return is
stopped, or left to the reconciliation if the server cannot be reached:
it never keeps the queue, so the rollback of a move whose new server is
gone is not held back.

Whoever closes the job sees how the step ended when the server says so:
its real exit status, not a step that "did not end". A hidden job's step
holds a scope of its own on the server, apart from the visible jobs of
the same instance. And a job whose closing hooks fetch something through
the panel starts them in a fresh attempt when its own has run long.

The steps run for real, on this machine, as in ``test_job_parking``.
"""
import contextlib
import json
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import asyncssh

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.queue_job.exception import JobError

from ..models import abstract_executor
from ..models.abstract_executor import DETACHED_HOLDS_QUEUE_MAX_S
from ..models.restore_instance_executor import RestoreInstanceExecutor
from ..models.transport import CommandResult
from .test_detached_step import _Apart, _local_transport
from .test_job_parking import _TRAPPED, _Parking, _ParkingCase, _StopCase


@contextlib.asynccontextmanager
async def _refused(_host):
    """The server refuses the connection."""
    raise ConnectionRefusedError(111, "Connection refused")
    yield  # pragma: no cover


def _wait_for(path, seconds=20):
    """Wait until *path* exists; return whether it does."""
    for _wait in range(int(seconds / 0.1)):
        if Path(path).exists():
            return True
        time.sleep(0.1)
    return False


class _CutOff(_Parking):
    """Closed by the reconciliation as a restore is: the cut step failed."""

    errors = None

    async def on_failure(self, results, errors):
        """Record the failure, what had run and why."""
        await super().on_failure(results, errors)
        self.errors = errors

    async def on_abort(self, exc):
        """Close the cut run as one whose step failed."""
        await self._fail_cut_off_run(exc)


class _HoldCase(_StopCase):
    """A started job whose executor closes a cut run through ``on_failure``."""

    def _class(self, plan, spend_at=None):
        """Return a ``_CutOff`` class of its own for one test."""
        return type("_TestCutOff", (_CutOff,), {
            "plan": plan, "spend_at": spend_at, "instances": (),
            "prepared": 0, "busy_once": False, "spent": False,
        })

    def _scope(self):
        """The job's queue."""
        return (None, self.host.id)

    def _busy(self):
        """Whether the job's queue is busy now."""
        return self.env["cloud.job"]._scope_busy(*self._scope())

    def _lost(self, **attrs):
        """Park the job with its step running, then delete its queue.job.

        :return: the executor class
        """
        cls = self._parked(**attrs)
        self._postponed()
        self.qjob.unlink()
        return cls

    def _closing(self, cls):
        """The one executor that closed the job."""
        closing = [executor for executor in cls.instances if executor.closed]
        self.assertEqual(len(closing), 1)
        return closing[0]

    def _ended(self):
        """Wait until the step apart recorded its end on the server."""
        self.assertTrue(_wait_for(f"{self.directory}/exit"))


class TestAStepLeftToEndHoldsItsQueue(_HoldCase):
    """Past the point of no return, the queue waits for the step."""

    def test_the_queue_waits_and_is_served_once_the_step_ended(self):
        """Busy while it runs, looked at every pass, served when it ends."""
        done = os.path.join(self.marks, "ended")
        cls = self._lost(
            apart=(
                f"echo Apart >> {self.runs}; echo line-1; sleep 6;"
                f" echo line-2; touch {done}; exit 3"
            ),
            _no_return_from="Apart",
        )
        Job = type(self.env["cloud.job"])
        self._reconcile(cls)
        self.assertEqual(self.job.state, "failed")
        row = self._row()
        self.assertTrue(row.holds_queue_since)
        # A server that answers is looked at again at the next pass.
        self.assertFalse(row.reconcile_tried_at)
        self.assertTrue(self._busy())
        self._age()
        self.assertIn(self.job.id, self.env["cloud.job"]._lost_job_ids())
        self._reconcile(cls)
        self.assertTrue(self._row().holds_queue_since)
        self.assertTrue(self._busy())

        self.assertTrue(_wait_for(done))
        self._ended()
        with patch.object(Job, "_schedule_dispatch", autospec=True) as dispatch:
            self._reconcile(cls)
        self.assertFalse(self._row())
        self.assertFalse(self._busy())
        self.assertTrue(any(
            {tuple(scope) for scope in call.args[1]} == {self._scope()}
            for call in dispatch.call_args_list
        ), dispatch.call_args_list)
        self.assertFalse(Path(self.directory).exists())
        log = self._log()
        # Said once while it ran, and once at its end.
        self.assertEqual(sum(
            "is past the point of no return and still runs" in line
            for line in log
        ), 1, log)
        self.assertEqual(log.count(
            "'Apart' ended on the server with exit status 3.",
        ), 1, log)
        self.assertIn("line-2", log)
        # Closed while the step ran: for its hook, it did not end.
        closing = self._closing(cls)
        self.assertIsNone(closing.closed[1]["Apart"]["exit_status"])
        self.assertTrue(closing.errors[0].startswith("'Apart' did not end"))

    def test_a_server_that_cannot_be_reached_holds_it_until_it_expires(self):
        """Not known to have ended: held, but no longer than the expiry."""
        cls = self._lost(_no_return_from="Apart")
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        self.assertEqual(self.job.state, "failed")
        row = self._row()
        self.assertTrue(row.holds_queue_since)
        self.assertTrue(row.reconcile_tried_at)
        self.assertTrue(self._busy())
        # A second pass that cannot reach it either keeps the first mark:
        # the expiry counts from it.
        self.env.cr.execute(
            "UPDATE cloud_job_checkpoint SET holds_queue_since ="
            " holds_queue_since - interval '100 seconds' WHERE job_id = %s",
            (self.job.id,),
        )
        self.env.invalidate_all()
        first = self._row().holds_queue_since
        self._retry_due()
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        self.assertEqual(self._row().holds_queue_since, first)
        self.env.cr.execute(
            "UPDATE cloud_job_checkpoint SET holds_queue_since ="
            " holds_queue_since - make_interval(secs => %s) WHERE job_id = %s",
            (DETACHED_HOLDS_QUEUE_MAX_S + 1, self.job.id),
        )
        self.env.invalidate_all()
        self.assertFalse(self._busy())

    def test_a_step_before_the_point_of_no_return_never_holds_it(self):
        """Its server gone, the queue goes on (a move's restore: below)."""
        cls = self._lost()
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        self.assertEqual(self.job.state, "failed")
        row = self._row()
        self.assertTrue(row)
        self.assertTrue(row.reconcile_tried_at)
        self.assertFalse(row.holds_queue_since)
        self.assertFalse(self._busy())

    def test_a_step_stopped_holds_nothing(self):
        """Before the point of no return, reached: stopped, the row gone."""
        cls = self._lost()
        self._reconcile(cls)
        self.assertFalse(self._row())
        self.assertFalse(self._busy())
        self.assertIn("trapped", self._log())

    def _ended_by_hand(self):
        """The job failed and its queue.job ended: only its server is left."""
        self.job.with_context(ic_skip_state_hook=True).write({"state": "failed"})
        if self.qjob.exists():
            self.qjob.write({"state": "failed"})
        self.env["cloud.job"].invalidate_model(["state"])

    def test_a_step_never_started_does_not_hold_it(self):
        """Parked before the step, the server gone: nothing of it runs."""
        cls = self._class(self._plan(), spend_at="step:1")
        cls._no_return_from = "Apart"
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self._postponed()
        self.qjob.unlink()
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        row = self._row()
        self.assertTrue(row)
        self.assertFalse(row.holds_queue_since)
        self.assertFalse(self._busy())

    def test_a_closed_row_whose_step_never_started_does_not_hold_it(self):
        """Closed by the attempt after the park, which never reached the server.

        A close writes ``parked = FALSE``: the row of a step never
        started reads as that of an attempt that died with it running.
        """
        cls = self._class(self._plan(), spend_at="step:1")
        cls._no_return_from = "Apart"
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self._row().write({"closed": True, "parked": False})
        self._ended_by_hand()
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        row = self._row()
        self.assertTrue(row)
        self.assertFalse(row.holds_queue_since)
        self.assertFalse(self._busy())

    def test_a_row_kept_for_a_directory_only_does_not_hold_it(self):
        """The step ended and was read; a process of it kept its directory."""
        cls = self._lost(_no_return_from="Apart")
        row = self._row()
        data = row._data()
        data["unclean"] = [1]
        row.write({"closed": True, "parked": False, "data": json.dumps(data)})
        self._ended_by_hand()
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        row = self._row()
        self.assertTrue(row)
        self.assertFalse(row.holds_queue_since)
        self.assertFalse(self._busy())

    def test_a_requeue_that_resumes_the_job_drops_the_mark(self):
        """A row marked and not closed, taken up again: it holds nothing now."""
        cls = self._parked(_no_return_from="Apart")
        self.env.cr.execute(
            "UPDATE cloud_job_checkpoint SET holds_queue_since ="
            " now() at time zone 'UTC' WHERE job_id = %s", (self.job.id,),
        )
        self.env.invalidate_all()
        self.assertTrue(self._row().holds_queue_since)
        cls(self.job, self.host)._resume(self._row())
        self.assertFalse(self._row().holds_queue_since)

    def test_a_mark_set_by_the_pass_that_gives_up_serves_the_queue(self):
        """The row marked and deleted in one pass: the queue is served."""
        cls = self._lost(_no_return_from="Apart")
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        # A row not marked yet, well past its cap.
        self.env.cr.execute(
            "UPDATE cloud_job_checkpoint SET holds_queue_since = NULL,"
            " step_started_at = (now() at time zone 'UTC') - interval '3 hours',"
            " lost_contact_since = NULL WHERE job_id = %s", (self.job.id,),
        )
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date - interval '3 hours'"
            " WHERE id = %s", (self.job.id,),
        )
        self._retry_due()
        Job = type(self.env["cloud.job"])
        with patch.object(
            Job, "_schedule_dispatch", autospec=True,
        ) as dispatch, mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        self.assertFalse(self._row())
        self.assertTrue(any(
            {tuple(scope) for scope in call.args[1]} == {self._scope()}
            for call in dispatch.call_args_list
        ), dispatch.call_args_list)


class TestAMoveRestoreUndoneDoesNotHoldItsQueue(_HoldCase):
    """A move's restore cut with its new server gone, the move then undone.

    The restore loads a copy nothing uses yet: its load is before any
    point of no return while the instance moves (``_loads_as_move``).
    The rollback removes the move's marks, but the step that ran is the
    same: the reconciliation that comes back to it later neither holds
    the queue of the instance, now in production on its source, nor
    leaves the load running.
    """

    def setUp(self):
        """The instance on its source, moving to the job's host."""
        super().setUp()
        self.source = self.env["cloud.host"].create({
            "name": "move-source",
            "ip_address": "192.0.2.98",
            "user": "odoo",
            "wildcard_domain": "source.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Move hold"})
        self.inst = self.env["cloud.instance"].create({
            "name": "move-hold",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.source.id,
        })
        self.inst.sudo().write({"move_target_host_id": self.host.id})
        self.job.write({
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restore_instance")], limit=1,
            ).id,
            "instance_id": self.inst.id,
            "payload": {"mode": "from_host", "as_move": True},
        })
        self.env.flush_all()

    def _scope(self):
        """The instance's queue."""
        return (self.inst.id, None)

    def _cut_then_undone(self):
        """The restore parks with its load running and is lost; then the
        reconciliation cannot reach the target, and the move is undone."""
        plan = [
            ("Stop Odoo service", f"echo Stop >> {self.runs}"),
            ("Restore database", _TRAPPED.format(runs=self.runs), {"detached": True}),
            ("Start Odoo service", f"echo Start >> {self.runs}"),
        ]

        def _spent(executor):
            """Spent once it follows the load."""
            return (
                executor._on_server_index is not None
                and executor._detached_offset > 0
            )

        with patch.object(
            RestoreInstanceExecutor, "before_execute", autospec=True,
        ), patch.object(
            RestoreInstanceExecutor, "get_commands", autospec=True,
            return_value=plan,
        ), patch.object(
            RestoreInstanceExecutor, "_budget_spent", autospec=True,
            side_effect=_spent,
        ):
            self.assertIsInstance(
                self._execute(RestoreInstanceExecutor),
                abstract_executor.JobParked,
            )
        self.assertTrue(Path(self.directory).exists())
        job_dir = self._row()._data().get("job_dir")
        if job_dir:
            self.addCleanup(subprocess.run, ["rm", "-rf", job_dir], check=False)
        self._postponed()
        self.qjob.unlink()
        with mute_logger("AbstractExecutor"):
            self._reconcile(RestoreInstanceExecutor, connect=_refused)
        self.assertEqual(self.job.state, "failed")
        self.assertTrue(self._row())
        self.assertFalse(self._row().holds_queue_since)
        # The rollback's clean-up of the target removes the move's marks.
        self.inst.sudo().write({"move_target_host_id": False})
        self.env.flush_all()
        self._retry_due()

    def test_the_target_still_gone(self):
        """Another pass that cannot reach it: no mark, the queue free."""
        self._cut_then_undone()
        with mute_logger("AbstractExecutor"):
            self._reconcile(RestoreInstanceExecutor, connect=_refused)
        row = self._row()
        self.assertTrue(row)
        self.assertFalse(row.holds_queue_since)
        self.assertFalse(self._busy())

    def test_the_target_back(self):
        """A pass that reaches it: the load is stopped, the row gone."""
        self._cut_then_undone()
        self._reconcile(RestoreInstanceExecutor)
        self.assertFalse(self._row())
        self.assertFalse(self._busy())
        self.assertFalse(Path(self.directory).exists())
        self.assertIn("trapped", self._log())


class TestTheCloseSeesHowTheStepEnded(_HoldCase):
    """The hook that closes a cut run gets the step's real end, when known."""

    def _ended_before_the_close(self, status):
        """The step ends on the server while nobody follows it.

        :return: the executor that closed the job
        """
        cls = self._lost(apart=(
            f"echo Apart >> {self.runs}; echo line-1; sleep 1; echo line-2;"
            f" exit {status}"
        ))
        self._ended()
        self._reconcile(cls)
        self.assertEqual(self.job.state, "failed")
        self.assertFalse(self._row())
        self.assertIn(
            f"'Apart' ended on the server with exit status {status}.",
            self._log(),
        )
        return self._closing(cls)

    def test_a_step_that_ended_well(self):
        """Exit 0: the hook sees it ended, with its stdout."""
        closing = self._ended_before_the_close(0)
        self.assertEqual(
            closing.closed[1]["Apart"],
            {"stdout": "line-1\nline-2", "exit_status": 0},
        )
        self.assertFalse(closing.errors[0].startswith("'Apart' did not end"))

    def test_a_step_that_failed(self):
        """Exit 3: the hook sees that status."""
        closing = self._ended_before_the_close(3)
        self.assertEqual(closing.closed[1]["Apart"]["exit_status"], 3)

    def test_a_step_still_running(self):
        """Past the point of no return and running: it did not end."""
        cls = self._lost(_no_return_from="Apart")
        self._reconcile(cls)
        closing = self._closing(cls)
        self.assertIsNone(closing.closed[1]["Apart"]["exit_status"])
        self.assertTrue(closing.errors[0].startswith("'Apart' did not end"))

    def test_a_server_that_cannot_be_reached(self):
        """Nothing known of it: it did not end."""
        cls = self._lost()
        with mute_logger("AbstractExecutor"):
            self._reconcile(cls, connect=_refused)
        closing = self._closing(cls)
        self.assertIsNone(closing.closed[1]["Apart"]["exit_status"])

    def test_the_step_in_course_is_taken_from_the_checkpoint(self):
        """Started apart, or not parked: the step ran when the attempt ended."""
        cls = self._class(self._plan(), spend_at="step:1")
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        row = self._row()
        self.assertTrue(row.parked)
        self.assertFalse(row.detached_started)
        executor = cls(self.job, self.host)
        executor._adopt_checkpoint(row)
        self.assertIsNone(executor._running_step)
        # The attempt that took it up died before it recorded the start.
        row.write({"parked": False})
        executor = cls(self.job, self.host)
        executor._adopt_checkpoint(self._row())
        self.assertEqual(executor._running_step, "Apart")
        row.write({"parked": True, "detached_started": True})
        executor = cls(self.job, self.host)
        executor._adopt_checkpoint(self._row())
        self.assertEqual(executor._running_step, "Apart")


class TestALostServerPastTheGrace(_ParkingCase):
    """A job that gives up on the server of its step holds its queue if past the point of no return."""

    def _losing(self):
        """A transport whose second read of the step loses the connection."""
        transport = _local_transport()
        real_run = transport.run.side_effect
        reads = {"n": 0}

        async def _run(command, timeout=3600):
            """Lose the connection on the second read."""
            if "ctl.sh read" in command:
                reads["n"] += 1
                if reads["n"] == 2:
                    raise asyncssh.ConnectionLost("Connection lost")
            return await real_run(command, timeout)

        transport.run.side_effect = _run
        return transport

    def _given_up(self, no_return):
        """Lose the server while the step runs, then past the grace fail.

        :return: the job's checkpoint
        """
        cls = self._class(self._plan())
        cls._no_return_from = no_return
        self.assertIsInstance(
            self._execute(cls, self._losing()), abstract_executor.JobParked,
        )
        self._row().write({
            "lost_contact_since": fields.Datetime.now() - timedelta(
                seconds=abstract_executor.DETACHED_CONTACT_GRACE_S + 1,
            ),
        })
        with mute_logger("AbstractExecutor"):
            self.assertIsInstance(
                self._execute(cls, connect=_refused), JobError,
            )
        return self._row()

    def test_past_the_point_of_no_return_it_holds(self):
        """The step may still run: the queue waits for it."""
        self.assertTrue(self._given_up("Apart").holds_queue_since)

    def test_before_it_it_does_not(self):
        """The 73 of ``start`` and the reconciliation cover the rest."""
        row = self._given_up(None)
        self.assertTrue(row)
        self.assertFalse(row.holds_queue_since)


class TestAStopThatFailsPastThePointHoldsTheQueue(_ParkingCase):
    """The job's own stop of its step leaves it alive: the queue waits."""

    def _failed_stop(self, no_return):
        """The step's read gets an answer it cannot use; its stop fails.

        :return: the job's checkpoint once the job failed
        """
        transport = _local_transport()
        real_run = transport.run.side_effect
        reads = {"n": 0}

        async def _run(command, timeout=3600):
            """A read that cannot be used, then a stop that fails."""
            if "ctl.sh read" in command:
                reads["n"] += 1
                if reads["n"] == 2:
                    raise RuntimeError("an answer that cannot be read")
            if "ctl.sh stop" in command:
                return CommandResult(stdout="still there", exit_status=1)
            return await real_run(command, timeout)

        transport.run.side_effect = _run
        cls = self._class(self._plan(apart=_TRAPPED.format(runs=self.runs)))
        cls._no_return_from = no_return
        with mute_logger("AbstractExecutor"):
            self.assertIsInstance(self._execute(cls, transport), JobError)
        return self._row()

    def test_past_the_point_of_no_return_it_holds(self):
        """Marked by the job itself, before its queue is served."""
        row = self._failed_stop("Apart")
        self.assertTrue(row.closed)
        self.assertTrue(row.holds_queue_since)

    def test_before_it_it_does_not(self):
        """Left to the reconciliation, the queue free."""
        row = self._failed_stop(None)
        self.assertTrue(row.closed)
        self.assertFalse(row.holds_queue_since)


class TestARowThatHoldsTheQueue(TransactionCase):
    """``_scope_busy`` and the checkpoint of an ended job."""

    def setUp(self):
        """A host with two instances, and the job types to end jobs of."""
        super().setUp()
        self.Job = self.env["cloud.job"]
        self.host = self.env["cloud.host"].create({
            "name": "hold-host",
            "ip_address": "192.0.2.96",
            "user": "odoo",
            "wildcard_domain": "hold.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Hold"})
        # One production per project: the other is its staging.
        self.inst, self.other = self.env["cloud.instance"].create([
            {
                "name": f"hold-{environment}",
                "project_id": project.id,
                "environment": environment,
                "host_id": self.host.id,
            }
            for environment in ("production", "staging")
        ])

    def _ended(self, code, inst=None, holds=True, state="failed"):
        """A job of type *code* ended in *state* with a checkpoint left.

        :return: the ``cloud.job``
        """
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        self.assertTrue(job_type, code)
        job = self.Job.sudo().create({
            "host_id": self.host.id,
            "instance_id": inst.id if inst else False,
            "job_type_id": job_type.id,
            "name": "Ended",
            "queue_job_uuid": f"hold-{code}-{inst.id if inst else 0}",
        })
        job.with_context(ic_skip_state_hook=True).write({"state": state})
        self.env["cloud.job.checkpoint"].sudo().create({
            "job_id": job.id,
            "host_id": self.host.id,
            "closed": True,
            "holds_queue_since": fields.Datetime.now() if holds else False,
        })
        self.env.flush_all()
        return job

    def test_it_holds_its_instance_s_queue_only(self):
        """Not another instance's, nor its host's."""
        self._ended("deploy_instance", self.inst)
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))
        self.assertFalse(self.Job._scope_busy(self.other.id, None))
        self.assertFalse(self.Job._scope_busy(None, self.host.id))

    def test_a_host_job_s_row_holds_the_host_s_queue_only(self):
        """Not the queue of an instance on that host."""
        self._ended("host_probe")
        self.assertTrue(self.Job._scope_busy(None, self.host.id))
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))

    def test_a_row_of_another_server_holds_nothing(self):
        """Not the server the instance lives on: a move's target, or the
        server it was recovered from."""
        job = self._ended("deploy_instance", self.inst)
        target = self.env["cloud.host"].create({
            "name": "hold-target",
            "ip_address": "192.0.2.99",
            "user": "odoo",
            "wildcard_domain": "hold-target.example.com",
        })
        self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", job.id)],
        ).write({"host_id": target.id})
        self.env.flush_all()
        self.assertFalse(self.Job._server_holder(self.inst.id, None))
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))

    def test_the_reconciliation_looks_at_rows_that_hold_first(self):
        """Before the rows of servers that do not answer, whatever their ids."""
        unheld = self._ended("deploy_instance", self.inst, holds=False)
        held = self._ended("deploy_instance", self.other)
        self.assertLess(unheld.id, held.id)
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date - interval '10 minutes'"
            " WHERE id IN %s", ((unheld.id, held.id),),
        )
        ids = self.Job._lost_job_ids()
        self.assertLess(ids.index(held.id), ids.index(unheld.id))

    def test_a_row_without_the_mark_holds_nothing(self):
        """A directory left, a staging not removed: the queue goes on."""
        self._ended("deploy_instance", self.inst, holds=False)
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))

    def test_a_hidden_job_s_row_holds_nothing(self):
        """Hidden jobs never count in a queue."""
        self._ended("instance_health", self.inst)
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))

    def test_a_cancelled_job_s_row_holds_too(self):
        """Whatever its end."""
        self._ended("deploy_instance", self.inst, state="cancelled")
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))

    def test_a_mark_past_its_expiry_holds_nothing(self):
        """A stopped cron or an executor gone does not hold it for good."""
        job = self._ended("deploy_instance", self.inst)
        self.env.cr.execute(
            "UPDATE cloud_job_checkpoint SET holds_queue_since ="
            " holds_queue_since - make_interval(secs => %s) WHERE job_id = %s",
            (DETACHED_HOLDS_QUEUE_MAX_S + 1, job.id),
        )
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))

    def test_the_waiting_job_says_what_it_waits_for(self):
        """The job queued behind it names it, until the row goes."""
        job = self._ended("deploy_instance", self.inst)
        self.assertEqual(self.Job._server_holder(self.inst.id, None), job.id)
        self.assertFalse(self.Job._server_holder(self.other.id, None))
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "rebuild_instance")], limit=1,
        )
        first, second = self.Job.sudo().create([
            {
                "host_id": self.host.id,
                "instance_id": self.inst.id,
                "job_type_id": job_type.id,
                "name": name,
            }
            for name in ("First", "Second")
        ])
        (first | second).with_context(ic_skip_state_hook=True).write(
            {"state": "queued"},
        )
        self.env.flush_all()
        rows = {row["id"]: row for row in (first | second)._format()}
        self.assertEqual(rows[first.id]["server_wait"], job.id)
        # Behind another entry: it says how many go before it.
        self.assertIsNone(rows[second.id]["server_wait"])
        self.assertEqual(rows[second.id]["queue_position"], 1)
        history = {row["id"]: row for row in first._format_history()}
        self.assertEqual(history[first.id]["server_wait"], job.id)
        self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", job.id)],
        ).unlink()
        self.assertIsNone(first._format()[0]["server_wait"])


class TestTheScopeOfAStepApart(TransactionCase):
    """A hidden job's step does not hold the scope of the visible ones."""

    def test_a_hidden_job_has_a_scope_of_its_own(self):
        """Per instance or host, and per type for the hidden ones."""
        host = self.env["cloud.host"].create({
            "name": "scope-host",
            "ip_address": "192.0.2.97",
            "user": "odoo",
            "wildcard_domain": "scope.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Scope"})
        inst = self.env["cloud.instance"].create({
            "name": "scope-inst",
            "project_id": project.id,
            "environment": "production",
            "host_id": host.id,
        })

        def scope(code, instance=None):
            """The scope a step apart of a job of type *code* holds."""
            job_type = self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            )
            self.assertTrue(job_type, code)
            job = self.env["cloud.job"].sudo().create({
                "host_id": host.id,
                "instance_id": instance.id if instance else False,
                "job_type_id": job_type.id,
                "name": code,
            })
            return _Apart(job, host)._detached_scope()

        self.assertEqual(scope("rebuild_instance", inst), f"instance:{inst.id}")
        self.assertEqual(
            scope("instance_health", inst), f"instance:{inst.id}:instance_health",
        )
        self.assertEqual(scope("host_probe"), "host")
        self.assertEqual(scope("docker_prune"), "host:docker_prune")


class _Room(_Parking):
    """Its closing hooks fetch through the panel: they want room.

    ``elapsed_s`` of the attempt have gone by when its steps end, of a
    request cut at ``limit_s`` (None: no limit).
    """

    _outcome_needs_room = True
    elapsed_s = 0
    limit_s = None

    def _start_clock(self):
        """The attempt started ``elapsed_s`` ago, under ``limit_s``."""
        super()._start_clock()
        self._attempt_started = time.monotonic() - self.elapsed_s
        self._attempt_limit = self.limit_s


class TestTheOutcomeGetsAFreshAttempt(_ParkingCase):
    """A job whose hooks fetch through the panel starts them with time."""

    def _room(self, elapsed_s, limit_s, needs_room=True, plan=None, spend_at=None):
        """Return an executor class whose attempts have run *elapsed_s*."""
        return type("_TestRoom", (_Room,), {
            "plan": plan or self._plan(), "spend_at": spend_at, "instances": (),
            "prepared": 0, "busy_once": False, "spent": False,
            "elapsed_s": elapsed_s, "limit_s": limit_s,
            "_outcome_needs_room": needs_room,
        })

    def _in_one_attempt(self, cls):
        """The job ends in its first attempt, with no park."""
        self.assertIsNone(self._execute(cls))
        self.assertEqual(len(cls.instances), 1)
        self.assertEqual(cls.instances[0].closed[0], "success")

    def test_an_attempt_that_ran_long_leaves_the_outcome_to_a_fresh_one(self):
        """200 s of 900: parked after the steps; the next one does not park."""
        cls = self._room(200, 900)
        parked = self._execute(cls)
        self.assertIsInstance(parked, abstract_executor.JobParked)
        self.assertIsNone(cls.instances[0].closed)
        self.assertEqual(self._row().next_index, 3)
        self.assertTrue(any(
            "The result is fetched in a fresh attempt" in line
            for line in self._log()
        ))
        # It ran as long by the hooks: it resumed there, and goes on.
        self.assertIsNone(self._execute(cls))
        self.assertEqual(cls.instances[1].closed[0], "success")
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self.assertFalse(self._row())

    def test_an_attempt_with_room_enough_does_not_park(self):
        """100 s of 900: more than 0.85 of the limit is left."""
        self._in_one_attempt(self._room(100, 900))

    def test_a_short_attempt_does_not_park(self):
        """50 s of 300: past 0.15 of it, but under a minute."""
        self._in_one_attempt(self._room(50, 300))

    def test_a_server_without_a_limit_does_not_park(self):
        """No limit, no budget: nothing cuts the hooks."""
        self._in_one_attempt(self._room(5000, None))

    def test_a_job_whose_hooks_need_no_room_does_not_park(self):
        """The default."""
        self._in_one_attempt(self._room(200, 900, needs_room=False))

    def test_a_plan_without_a_step_apart_does_not_park(self):
        """No checkpoint: the hooks run in the attempt, however long it ran."""
        plan = tuple(step for step in self._plan() if step[0] != "Apart")
        self._in_one_attempt(self._room(200, 900, plan=plan))
        self.assertFalse(self._row())

    def test_a_run_a_step_cut_short_closes_in_its_attempt(self):
        """A failed step stopped the run: no closing hook fetches anything."""
        cls = self._room(200, 900, plan=self._plan(stop_first=True))
        self.assertIsInstance(self._execute(cls), JobError)
        self.assertEqual(len(cls.instances), 1)
        self.assertEqual(cls.instances[0].closed[0], "failure")
        self.assertEqual(self._runs(), ["First"])

    def test_an_attempt_resumed_halfway_still_leaves_it_to_a_fresh_one(self):
        """Resumed before its last steps, it is not the fresh attempt."""
        cls = self._room(200, 900, spend_at="step:1")
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self.assertEqual(self._row().next_index, 1)
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self.assertEqual(self._row().next_index, 3)
        self.assertIsNone(cls.instances[1].closed)
        self.assertIsNone(self._execute(cls))
        self.assertEqual(cls.instances[2].closed[0], "success")
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
