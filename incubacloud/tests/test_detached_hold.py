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
import os
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
from .test_detached_step import _Apart, _local_transport
from .test_job_parking import _Parking, _ParkingCase, _StopCase


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
        self.env.cr.execute(
            "UPDATE cloud_job_checkpoint SET holds_queue_since ="
            " holds_queue_since - make_interval(secs => %s) WHERE job_id = %s",
            (DETACHED_HOLDS_QUEUE_MAX_S + 1, self.job.id),
        )
        self.env.invalidate_all()
        self.assertFalse(self._busy())

    def test_a_step_before_the_point_of_no_return_never_holds_it(self):
        """A move's restore: its server gone, the rollback goes on."""
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
