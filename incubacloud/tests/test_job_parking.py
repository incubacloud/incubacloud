"""A job leaves its step running on the server and goes on in a later attempt.

On Odoo.sh a request is cut at 900 s and a build takes longer. An attempt
of a job with a step marked ``detached`` runs for its budget, then parks
(``JobParked``): its checkpoint says where it is, the step goes on on the
server, and queue_job runs the job again five seconds later. The next
attempt, through ``cloud.job.execute``, takes the job up where the first
left it: no step runs twice, no line of the log is lost or repeated.

The steps run for real, on this machine, through ``bash``: the transport
is a mock of ``SSHTransport`` whose commands run locally. Registry test
mode: the durable cursors ride on the test's transaction.
"""
import contextlib
import inspect
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import asyncssh

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.queue_job.exception import JobError, RetryableJobError

from ..models import abstract_executor
from ..models.abstract_executor import (
    DETACHED_BUSY_RETRY_S,
    detached_dir,
)
from ..models.cloud_host import CloudHost
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from .test_detached_step import _Apart, _local_transport


class _Died(BaseException):
    """The worker's process died: nothing of the attempt runs after this."""


class _Parking(_Apart):
    """The test's plan; its first attempt finds its budget spent where told.

    ``spend_at`` names the point: ``step:<index>`` (before that step),
    ``follow`` (while it follows a step apart, once it read some of its
    log) or ``hooks`` (before the closing hooks). Attempts that resume the
    job have time enough.
    """

    spend_at = None
    #: Every executor ``cloud.job.execute`` built, in order.
    instances = ()
    #: How many times ``before_execute`` ran.
    prepared = 0
    #: Set to make the closing check ask for a retry from the start, once.
    busy_once = False
    #: Whether an attempt found its budget spent already.
    spent = False
    #: Whether this attempt's steps are over.
    _steps_over = False

    def __init__(self, *args, **kwargs):
        """Record the executor, as the test's handle on each attempt."""
        super().__init__(*args, **kwargs)
        type(self).instances = (*type(self).instances, self)

    async def before_execute(self, transport):
        """Count the preparations: a resumed attempt makes none."""
        type(self).prepared += 1

    def _budget_spent(self):
        """Spent at ``spend_at``, the first time the job gets there."""
        if type(self).spent or self.spend_at is None:
            return False
        if self._on_server_index is not None:
            point = "follow" if self._detached_offset > 0 else None
        elif self._steps_over:
            point = "hooks"
        else:
            point = f"step:{len(self._step_results or {})}"
        type(self).spent = point == self.spend_at
        return type(self).spent

    async def _run_steps(self, transport, commands):
        """The steps; then the attempt is at its closing hooks."""
        outcome = await super()._run_steps(transport, commands)
        self._steps_over = True
        return outcome

    def parse_results(self, results):
        """Ask for a retry from the start once, when told to."""
        if type(self).busy_once:
            type(self).busy_once = False
            raise RetryableJobError("the crons are busy", seconds=1)
        return super().parse_results(results)


class _ParkingCase(TransactionCase):
    """A job of a host whose steps run on this machine, run by ``execute``."""

    def setUp(self):
        """The host, its job, and a place for what the steps leave."""
        super().setUp()
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        self.host = self.env["cloud.host"].create({
            "name": "parking-host",
            "ip_address": "192.0.2.95",
            "user": "odoo",
            "wildcard_domain": "parking.example.com",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1,
        )
        self.qjob = self.env["queue.job"].sudo().create({
            "uuid": "parking-qj",
            "name": "parking",
            "state": "started",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        self.job = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": job_type.id,
            "name": "Parking",
            "queue_job_uuid": self.qjob.uuid,
            "queue_job_id": self.qjob.id,
        })
        self.env.flush_all()
        self.marks = tempfile.mkdtemp()
        self.runs = os.path.join(self.marks, "runs")
        self.addCleanup(self._clean_server)

    def _clean_server(self):
        """Stop and remove what a test left on this machine."""
        uuids = {"parking-qj", "parking-qj-2", self.job.queue_job_uuid}
        for uuid in uuids:
            for index in range(4):
                directory = detached_dir(self.env, self.job.id, uuid, index)
                if Path(f"{directory}/ctl.sh").exists():
                    subprocess.run(
                        ["bash", f"{directory}/ctl.sh", "stop", directory, "0"],
                        check=False, capture_output=True,
                    )
                subprocess.run(["rm", "-rf", directory], check=False)
        for executor in _Parking.instances:
            if executor._job_dir_path:
                subprocess.run(["rm", "-rf", executor._job_dir_path], check=False)
        subprocess.run(["rm", "-rf", self.marks], check=False)
        with contextlib.suppress(ChildProcessError):
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass

    def _plan(self, apart=None, stop_first=False):
        """Three steps; the second runs apart and writes three lines slowly.

        Each step notes in ``runs`` that it ran.

        :param str apart: the second step's command instead
        :param bool stop_first: the first step fails and stops the run
        """
        first = f"echo First >> {self.runs}; echo first-out"
        if stop_first:
            first += "; exit 2"
        return (
            ("First", first, {"stop_on_failure": True} if stop_first else {}),
            ("Apart", apart or (
                f"echo Apart >> {self.runs}; echo line-1; sleep 0.4;"
                " echo line-2; sleep 0.4; echo line-3"
            ), {"detached": True}),
            ("Last", f"echo Last >> {self.runs}; echo last-out"),
        )

    def _class(self, plan, spend_at=None):
        """Return an executor class of its own for one test."""
        return type("_TestParking", (_Parking,), {
            "plan": plan, "spend_at": spend_at, "instances": (),
            "prepared": 0, "busy_once": False, "spent": False,
        })

    @contextlib.contextmanager
    def _connected(self, executor_cls, transport=None, connect=None):
        """``execute`` runs *executor_cls*, connected to this machine."""
        transport = transport or _local_transport()

        @contextlib.asynccontextmanager
        async def _connect(_host):
            yield transport

        Job = type(self.env["cloud.job"])
        with patch.object(
            Job, "_get_executor", autospec=True, return_value=executor_cls,
        ), patch.object(
            CloudHost, "get_transport", autospec=True,
            side_effect=connect or _connect,
        ):
            yield

    def _execute(self, executor_cls, transport=None, connect=None):
        """Run one attempt of the job; return what it raised, if anything."""
        with self._connected(executor_cls, transport, connect):
            try:
                self.job.execute()
            except (RetryableJobError, JobError, _Died) as exc:
                return exc
        return None

    def _dir(self, index, uuid=None):
        """The directory step *index* of the job runs apart in."""
        return detached_dir(
            self.env, self.job.id, uuid or self.job.queue_job_uuid, index,
        )

    def _runs(self):
        """The steps that ran, in order."""
        if not Path(self.runs).exists():
            return []
        return Path(self.runs).read_text().split()

    def _log(self):
        """The job's log, one line per chunk."""
        return self.env["cloud.job.log.chunk"].search(
            [("job_id", "=", self.job.id)], order="id",
        ).mapped("content")

    def _row(self):
        """The job's checkpoint, as the database holds it now."""
        row = self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", self.job.id)],
        )
        row.invalidate_recordset()
        return row

    def _assert_lines_once(self, *lines):
        """Each of *lines* is in the job log exactly once."""
        log = self._log()
        for line in lines:
            self.assertEqual(log.count(line), 1, f"{line!r} in {log}")


class TestParkAndResume(_ParkingCase):
    """Two attempts on one checkpoint run each step once."""

    def test_two_attempts_on_one_checkpoint_repeat_no_step(self):
        """Parked while it follows the step: the next one reads on from there."""
        cls = self._class(self._plan(), spend_at="follow")
        parked = self._execute(cls)
        self.assertIsInstance(parked, abstract_executor.JobParked)
        self.assertEqual(parked.seconds, abstract_executor.PARK_GAP_SECONDS)
        self.assertTrue(parked.ignore_retry)
        first = cls.instances[0]
        row = self._row()
        self.assertTrue(row.parked)
        self.assertTrue(row.parked_until)
        self.assertEqual(row.next_index, 1)
        self.assertTrue(row.detached_started)
        self.assertGreater(row.log_offset, 0)
        self.assertEqual(list(row._data()["results"]), ["First"])
        self.assertIsNone(first.closed)
        # The step goes on, with the staging it reads.
        self.assertTrue(Path(self._dir(1)).exists())
        self.assertTrue(Path(first._job_dir_path).exists())
        self.assertIn(
            "The step keeps running on the server; the panel reconnects in a"
            " few seconds.", self._log(),
        )

        self.assertIsNone(self._execute(cls))
        second = cls.instances[1]
        self.assertEqual(cls.prepared, 1)
        self.assertEqual(second._job_dir_path, first._job_dir_path)
        self.assertEqual(second.closed[0], "success")
        self.assertEqual(list(second.closed[1]), ["First", "Apart", "Last"])
        self.assertEqual(
            second.closed[1]["Apart"],
            {"stdout": "line-1\nline-2\nline-3", "exit_status": 0},
        )
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self._assert_lines_once(
            "first-out", "line-1", "line-2", "line-3", "last-out",
            "Running: Apart",
        )
        self.assertFalse(self._row())
        self.assertFalse(Path(first._job_dir_path).exists())
        self.assertFalse(Path(self._dir(1)).exists())

    def test_an_attempt_parks_between_two_steps(self):
        """Spent after a step: the next attempt starts at the one after it."""
        cls = self._class(self._plan(), spend_at="step:2")
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        row = self._row()
        self.assertEqual(row.next_index, 2)
        self.assertFalse(row.detached_started)
        self.assertIn(
            "This attempt's time is up: the job goes on in a few seconds.",
            self._log(),
        )
        self.assertEqual(self._runs(), ["First", "Apart"])
        self.assertIsNone(self._execute(cls))
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self.assertEqual(cls.instances[1].closed[0], "success")
        self.assertEqual(list(cls.instances[1].closed[1]), ["First", "Apart", "Last"])

    def test_an_attempt_parks_before_the_hooks(self):
        """Every step ran: the next attempt runs none, and closes the job."""
        cls = self._class(self._plan(), spend_at="hooks")
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self.assertIsNone(cls.instances[0].closed)
        self.assertEqual(self._row().next_index, 3)
        self.assertIsNone(self._execute(cls))
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self.assertEqual(cls.instances[1].closed[0], "success")
        self.assertFalse(self._row())
        self.env.cr.execute(
            "SELECT count(*) FROM cloud_job_outcome WHERE job_id = %s",
            (self.job.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (1,))

    def test_a_run_a_step_cut_stays_cut_after_a_park(self):
        """The steps a failure cut off are not run by the next attempt."""
        cls = self._class(self._plan(stop_first=True), spend_at="hooks")
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self.assertIsInstance(self._execute(cls), JobError)
        self.assertEqual(self._runs(), ["First"])
        self.assertEqual(cls.instances[1].closed[0], "failure")
        self.assertEqual(list(cls.instances[1].closed[1]), ["First"])
        self.assertFalse(self._row())

    def test_a_retry_that_is_not_a_park_starts_over(self):
        """The crons were busy: the job runs again from its first step."""
        cls = self._class(self._plan(), spend_at="follow")
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        cls.busy_once = True
        exc = self._execute(cls)
        self.assertIsInstance(exc, RetryableJobError)
        self.assertNotIsInstance(exc, abstract_executor.JobParked)
        self.assertFalse(self._row())
        self.assertFalse(Path(cls.instances[0]._job_dir_path).exists())
        self.assertIsNone(self._execute(cls))
        self.assertEqual(cls.prepared, 2)
        self.assertEqual(
            self._runs(), ["First", "Apart", "Last", "First", "Apart", "Last"],
        )
        self.assertEqual(cls.instances[2].closed[0], "success")

    def test_a_rebuilds_update_apart_held_by_the_crons_starts_over(self):
        """«Update changed modules» run apart exits 75 (``cron_guard.sh``:
        nothing was updated) in the attempt that took it up after a park:
        the rebuild's own ``parse_results`` asks for a retry from the start
        five minutes later, the checkpoint and the step's directory go, and
        the next attempt runs every step again (phase 3, 12-nov-2026)."""
        from ..models.rebuild_instance_executor import RebuildInstanceExecutor

        update = "Update changed modules"
        plan = list(self._plan(apart=(
            f"echo Update >> {self.runs}; echo line-1; sleep 0.4;"
            " echo line-2; exit 75"
        )))
        plan[1] = (update, plan[1][1], {"stop_on_failure": True, "detached": True})
        cls = self._class(tuple(plan), spend_at="follow")
        cls.parse_results = RebuildInstanceExecutor.parse_results
        cls._first_attempt_started_at = RebuildInstanceExecutor._first_attempt_started_at
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        exc = self._execute(cls)
        self.assertIsInstance(exc, RetryableJobError)
        self.assertNotIsInstance(exc, abstract_executor.JobParked)
        self.assertEqual(exc.seconds, 300)
        self.assertTrue(exc.ignore_retry)
        self.assertFalse(self._row())
        self.assertFalse(Path(self._dir(1)).exists())
        self.assertFalse(Path(cls.instances[0]._job_dir_path).exists())
        self.assertEqual(self._runs(), ["First", "Update"])
        self.assertEqual(cls.prepared, 1)
        plan[1] = (update, plan[1][1].replace("exit 75", "exit 0"), plan[1][2])
        cls.plan = tuple(plan)
        self.assertIsNone(self._execute(cls))
        self.assertEqual(cls.prepared, 2)
        self.assertEqual(
            self._runs(), ["First", "Update", "First", "Update", "Last"],
        )
        self.assertEqual(cls.instances[2].closed[0], "success")

    def test_the_budget_counts_from_the_start_of_the_attempt(self):
        """0.6 of the limit from ``_start_clock``; none without a limit."""
        executor = object.__new__(_Apart)
        with patch.object(
            abstract_executor, "attempt_budget_seconds", return_value=0,
        ):
            executor._start_clock()
        self.assertIsNone(executor._budget_ends)
        self.assertFalse(executor._budget_spent())
        with patch.object(
            abstract_executor, "attempt_budget_seconds", return_value=540,
        ), patch.object(abstract_executor.time, "monotonic", return_value=1000.0):
            executor._start_clock()
            self.assertEqual(executor._budget_ends, 1540.0)
            self.assertFalse(executor._budget_spent())
        with patch.object(abstract_executor.time, "monotonic", return_value=1540.0):
            self.assertTrue(executor._budget_spent())

    def test_another_job_s_live_step_parks_the_job(self):
        """73: the job parks 30 s at that step, which it starts next time."""
        plan = self._plan()
        cls = self._class(plan)
        transport = _local_transport()
        real_run = transport.run.side_effect
        busy = {"left": 1}

        async def _busy_once(command, timeout=3600):
            """The first start finds another job's step there."""
            if "detached_step.sh start" in command and busy["left"]:
                busy["left"] -= 1
                return abstract_executor.CommandResult(
                    stdout="[incubacloud] ERROR: another step still runs for"
                           " host on this server: job 12 Build",
                    exit_status=abstract_executor.DETACHED_EXIT_BUSY,
                )
            return await real_run(command, timeout)

        transport.run.side_effect = _busy_once
        exc = self._execute(cls, transport)
        self.assertIsInstance(exc, abstract_executor.JobParked)
        self.assertEqual(exc.seconds, DETACHED_BUSY_RETRY_S)
        row = self._row()
        self.assertTrue(row.parked)
        self.assertEqual(row.next_index, 1)
        self.assertFalse(row.detached_started)
        self.assertTrue(any("job 12 Build" in line for line in self._log()))
        self.assertIsNone(self._execute(cls, transport))
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])


class TestAWorkerDiesAtEachBoundary(_ParkingCase):
    """The worker dies; queue_job runs the job again, which resumes it."""

    def _dies(self, cls, method, when, before=False):
        """Run an attempt that dies in *method* once *when* says so.

        Nothing of the dying attempt runs after it: no cleanup of the
        staging, no stop of the step.

        :param str method: the executor's method to wrap
        :param when: ``(executor, args, kwargs, result) -> bool``, asked
            after the method ran (*result* None when *before*)
        :param bool before: die instead of running the method
        """
        original = getattr(_Parking, method)

        async def _async(self_, *args, **kwargs):
            """The async method, then maybe death."""
            if before and when(self_, args, kwargs, None):
                raise _Died()
            result = await original(self_, *args, **kwargs)
            if not before and when(self_, args, kwargs, result):
                raise _Died()
            return result

        def _sync(self_, *args, **kwargs):
            """The method, then maybe death."""
            if before and when(self_, args, kwargs, None):
                raise _Died()
            result = original(self_, *args, **kwargs)
            if not before and when(self_, args, kwargs, result):
                raise _Died()
            return result

        wrapper = _async if inspect.iscoroutinefunction(original) else _sync

        async def _nothing(*_args, **_kwargs):
            """A dead process cleans nothing up."""

        with patch.object(cls, method, wrapper), patch.object(
            cls, "_discard_staging", _nothing,
        ), patch.object(cls, "_stop_detached", _nothing):
            self.assertIsInstance(self._execute(cls), _Died)

    def _resumed_to_success(self, cls):
        """The next attempt resumes the job and closes it."""
        self.assertIsNone(self._execute(cls))
        last = cls.instances[-1]
        self.assertTrue(last._resumed)
        self.assertEqual(last.closed[0], "success")
        self.assertFalse(self._row())

    def test_after_the_checkpoint_is_opened(self):
        """Before any step: every step runs, once."""
        cls = self._class(self._plan())
        self._dies(cls, "_open_checkpoint", lambda *_a: True)
        self._resumed_to_success(cls)
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self._assert_lines_once("line-1", "line-2", "line-3")

    def test_after_a_step_before_its_record(self):
        """The step ran and was not recorded: it runs again."""
        cls = self._class(self._plan())
        self._dies(cls, "_save_checkpoint_step", lambda *_a: True, before=True)
        self._resumed_to_success(cls)
        self.assertEqual(self._runs(), ["First", "First", "Apart", "Last"])

    def test_after_the_start_before_its_record(self):
        """The step was started apart: the next start does nothing."""
        cls = self._class(self._plan())
        self._dies(
            cls, "_write_checkpoint",
            lambda _s, args, _k, _r: bool(args[0].get("detached_started")),
            before=True,
        )
        self._resumed_to_success(cls)
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self._assert_lines_once("line-1", "line-2", "line-3")

    def test_while_it_follows_the_step(self):
        """Lines read and not written yet are read again, none twice."""
        cls = self._class(self._plan(apart=(
            f"echo Apart >> {self.runs}; echo line-1; sleep 1.5;"
            " echo line-2; sleep 1.5; echo line-3"
        )))
        seen = {"offset": 0, "reads": 0}

        def _second_read(_self, _args, _kwargs, _result):
            """Dies right after the second read that brought lines."""
            if _self._detached_offset == seen["offset"]:
                return False
            seen["offset"] = _self._detached_offset
            seen["reads"] += 1
            return seen["reads"] == 2

        self._dies(cls, "_read_detached", _second_read)
        self.assertTrue(self._row().detached_started)
        self._resumed_to_success(cls)
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self._assert_lines_once("line-1", "line-2", "line-3", "Running: Apart")

    def test_after_the_step_ended_before_its_record(self):
        """The step ended and its record did not: its directory is still there.

        Its result is recorded before its directory is removed, so the
        next attempt reads its end there, and runs it neither again nor
        into a directory that is gone.
        """
        cls = self._class(self._plan())
        self._dies(
            cls, "_save_checkpoint_step",
            lambda _s, args, _k, _r: args[0] == 2, before=True,
        )
        self.assertTrue(Path(self._dir(1)).exists())
        self._resumed_to_success(cls)
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self._assert_lines_once("line-1", "line-2", "line-3")
        self.assertFalse(Path(self._dir(1)).exists())

    def test_after_the_record_before_the_directory_goes(self):
        """Recorded, not removed: the next attempt removes it, and goes on."""
        cls = self._class(self._plan())
        self._dies(cls, "_clean_detached", lambda *_a: True, before=True)
        self.assertEqual(self._row().next_index, 2)
        self.assertTrue(Path(self._dir(1)).exists())
        self._resumed_to_success(cls)
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self.assertFalse(Path(self._dir(1)).exists())


class TestLostContact(_ParkingCase):
    """The server of a running step cannot be reached: the job waits for it."""

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

    @staticmethod
    @contextlib.asynccontextmanager
    async def _refused(_host):
        """The server refuses the connection."""
        raise ConnectionRefusedError(111, "Connection refused")
        yield  # pragma: no cover

    def test_a_lost_connection_parks_and_the_step_goes_on(self):
        """30 s, then the next attempt reads on; the loss is forgotten."""
        cls = self._class(self._plan())
        exc = self._execute(cls, self._losing())
        self.assertIsInstance(exc, abstract_executor.JobParked)
        self.assertEqual(exc.seconds, abstract_executor.DETACHED_RECONNECT_GAP_S)
        row = self._row()
        self.assertTrue(row.parked)
        self.assertTrue(row.lost_contact_since)
        self.assertTrue(row.detached_started)
        self.assertEqual(row.next_index, 1)
        self.assertTrue(Path(self._dir(1)).exists())
        self.assertTrue(Path(cls.instances[0]._job_dir_path).exists())
        self.assertTrue(any("Lost contact with the server" in line for line in self._log()))

        # Still unreachable: parked again, the loss dated from the first.
        since = row.lost_contact_since
        self.assertIsInstance(self._execute(cls, connect=self._refused), abstract_executor.JobParked)
        self.assertEqual(self._row().lost_contact_since, since)

        self.assertIsNone(self._execute(cls))
        self.assertEqual(cls.instances[-1].closed[0], "success")
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self._assert_lines_once("line-1", "line-2", "line-3")
        self.assertFalse(self._row())

    def test_a_resumed_attempt_that_cannot_connect_between_steps_parks(self):
        """No step runs on the server: the job waits for it all the same."""
        cls = self._class(self._plan(), spend_at="step:2")
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self.assertFalse(self._row().detached_started)
        with mute_logger("AbstractExecutor"):
            exc = self._execute(cls, connect=self._refused)
        self.assertIsInstance(exc, abstract_executor.JobParked)
        self.assertEqual(exc.seconds, abstract_executor.DETACHED_RECONNECT_GAP_S)
        row = self._row()
        self.assertTrue(row.parked)
        self.assertTrue(row.lost_contact_since)
        self.assertEqual(row.next_index, 2)
        self.assertIsNone(self._execute(cls))
        self.assertEqual(self._runs(), ["First", "Apart", "Last"])
        self.assertEqual(cls.instances[-1].closed[0], "success")
        self.assertFalse(self._row())

    def test_past_the_grace_the_job_fails_and_leaves_the_step(self):
        """10 minutes without the server: failed, the row kept for later."""
        cls = self._class(self._plan())
        self.assertIsInstance(self._execute(cls, self._losing()), abstract_executor.JobParked)
        self._row().write({
            "lost_contact_since": fields.Datetime.now() - timedelta(
                seconds=abstract_executor.DETACHED_CONTACT_GRACE_S + 1,
            ),
        })
        with mute_logger("AbstractExecutor"):
            exc = self._execute(cls, connect=self._refused)
        self.assertIsInstance(exc, JobError)
        row = self._row()
        self.assertTrue(row)
        self.assertFalse(row.parked)
        self.assertTrue(any(
            "the step is left to the reconciliation" in line for line in self._log()
        ))


class TestAParkedPostponeIsNotMirrored(TransactionCase):
    """queue_job stores the park as a postpone; the cloud job stays started."""

    def setUp(self):
        """A started job and its queue job."""
        super().setUp()
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        host = self.env["cloud.host"].create({
            "name": "postpone-host",
            "ip_address": "192.0.2.96",
            "user": "odoo",
            "wildcard_domain": "postpone.example.com",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1,
        )
        self.qjob = self.env["queue.job"].sudo().create({
            "uuid": "postpone-qj",
            "name": "postpone",
            "state": "started",
            "priority": 10,
            "retry": 2,
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        self.cjob = self.env["cloud.job"].sudo().create({
            "host_id": host.id,
            "job_type_id": job_type.id,
            "name": "Postpone",
            "queue_job_uuid": self.qjob.uuid,
            "queue_job_id": self.qjob.id,
        })
        self.cjob.with_context(ic_skip_state_hook=True).write({"state": "started"})
        self.env.flush_all()

    def _checkpoint(self, parked=True, uuid="postpone-qj"):
        """The cloud job's checkpoint."""
        self.env["cloud.job.checkpoint"].sudo().create({
            "job_id": self.cjob.id,
            "queue_job_uuid": uuid,
            "parked": parked,
        })
        self.env.flush_all()

    def _postpone(self):
        """What queue_job's ``retry_postpone`` stores."""
        self.qjob.write({
            "state": "pending",
            "eta": "2026-11-11 08:00:05",
            "date_started": False,
            "priority": 10,
            "retry": 2,
            "result": "Parked",
        })
        self.env["cloud.job"].invalidate_model(["state"])

    def test_a_parked_job_stays_started_and_comes_back_first(self):
        """Priority 0 and no attempt counted."""
        self._checkpoint()
        self._postpone()
        self.assertEqual(self.qjob.state, "pending")
        self.assertEqual(self.qjob.priority, abstract_executor.PARKED_RETURN_PRIORITY)
        self.assertEqual(self.qjob.retry, 0)
        self.assertEqual(self.cjob.state, "started")

    def test_a_postpone_that_is_not_a_park_is_mirrored(self):
        """A row not parked, or none: the cloud job waits ``pending``."""
        self._checkpoint(parked=False)
        self._postpone()
        self.assertEqual(self.cjob.state, "pending")
        self.assertEqual(self.qjob.priority, 10)
        self.assertEqual(self.qjob.retry, 2)

    def test_a_job_with_a_row_ended_by_hand_is_left_to_the_reconciliation(self):
        """Its worker died mid-attempt (row not parked), then Cancel by hand."""
        self._checkpoint(parked=False)
        # What queue_job's requeue of a dead job leaves, by SQL.
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'pending' WHERE id = %s", (self.qjob.id,),
        )
        self.qjob.invalidate_recordset()
        self.qjob.write({"state": "cancelled"})
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(self.qjob.state, "cancelled")
        self.assertEqual(self.cjob.state, "started")

    def test_another_queue_job_s_row_is_not_a_park(self):
        """A row another queue job wrote is the reconciliation's."""
        self._checkpoint(uuid="another-qj")
        self._postpone()
        self.assertEqual(self.cjob.state, "pending")


#: What the log of a job whose type has no executor any more says.
_NO_EXECUTOR = (
    "This job's type has no executor in this server any more: nothing can"
    " stop or remove what it left on its server. Check the server's /tmp by"
    " hand."
)

#: A step apart that says when it started, then waits for its stop and
#: says it trapped it.
_TRAPPED = (
    "trap 'echo trapped; exit 7' TERM; echo Apart >> {runs}; echo line-1;"
    " sleep 300 & wait"
)


class _StopCase(_ParkingCase):
    """A started job whose first attempt parks while its step runs."""

    def setUp(self):
        """The job runs, as queue_job leaves it once it started it."""
        super().setUp()
        self.job.with_context(ic_skip_state_hook=True).write({"state": "started"})
        self.env.flush_all()
        self.directory = self._dir(1)

    def _parked(self, apart=None, **attrs):
        """Run the first attempt, which parks with its step running.

        :return: the executor class
        """
        cls = self._class(
            self._plan(apart=apart or _TRAPPED.format(runs=self.runs)),
            spend_at="follow",
        )
        for name, value in attrs.items():
            setattr(cls, name, value)
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self.assertTrue(Path(self.directory).exists())
        return cls

    def _postponed(self):
        """What queue_job's ``retry_postpone`` stores for the park."""
        self.qjob.write({
            "state": "pending", "eta": "2026-11-11 08:00:05",
            "date_started": False, "result": "Parked",
        })
        self.env["cloud.job"].invalidate_model(["state"])

    def _age(self):
        """Put the job past the reconciliation's margin."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date - interval '10 minutes'"
            " WHERE id = %s", (self.job.id,),
        )
        self.env.cr.execute(
            "UPDATE queue_job SET date_created = date_created - interval"
            " '10 minutes', date_enqueued = (now() at time zone 'UTC')"
            " - interval '10 minutes',"
            " date_done = date_done - interval '10 minutes',"
            " date_cancelled = date_cancelled - interval '10 minutes'"
            " WHERE uuid = %s", (self.job.queue_job_uuid,),
        )
        self.env.invalidate_all()

    def _retry_due(self):
        """Put the reconciliation's last try at the row past its spacing."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_job_checkpoint SET reconcile_tried_at ="
            " reconcile_tried_at - interval '11 minutes' WHERE job_id = %s",
            (self.job.id,),
        )
        self.env.invalidate_all()

    def _reconcile(self, cls, connect=None):
        """One pass of the reconciliation, with *cls* as the job's executor."""
        self._age()
        with self._connected(cls, connect=connect), patch.object(
            abstract_executor.executor_registry, "get", return_value=cls,
        ):
            self.env["cloud.job"]._cron_reconcile_lost_jobs()
        self.env.invalidate_all()

    def _given_up_alert(self):
        """The alert the reconciliation raises when it gives the job up."""
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", "job_cleanup_given_up"), ("job_id", "=", self.job.id),
        ])


class TestAParkedJobIsStopped(_StopCase):
    """Windows 2 and 3: a stop while it is parked closes it when it is back."""

    def _stopping(self):
        """What the lists and the history say: whether the job still stops."""
        self.env.invalidate_all()
        rows = self.job._format() + self.job._format_history()
        return {row["stopping"] for row in rows}

    def test_a_job_cancelled_while_parked_reads_stopping_until_it_closed(self):
        """Cancelled while parked, the job stops until it comes back and
        closes; a worker found dead before that leaves it stopping until
        its checkpoint is closed."""
        cls = self._parked()
        self.assertEqual(self._stopping(), {False})
        self.job.cancel_job()
        self.env.flush_all()
        self._postponed()
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(self._stopping(), {True})
        # Its worker found dead three times: queue_job fails it by SQL,
        # and its checkpoint still says what runs on the server.
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'failed' WHERE id = %s", (self.qjob.id,),
        )
        self.assertFalse(self._row().closed)
        self.assertEqual(self._stopping(), {True})
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'started' WHERE id = %s", (self.qjob.id,),
        )
        self.env.invalidate_all()
        with self._connected(cls):
            self.assertEqual(
                self.job.execute(), abstract_executor.CANCELLED_WHILE_RUNNING,
            )
        self.qjob.write({
            "state": "done", "result": abstract_executor.CANCELLED_WHILE_RUNNING,
        })
        self.assertFalse(self._row())
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(self._stopping(), {False})
        self.assertFalse(self._given_up_alert())

    def test_a_stop_while_parked_stops_the_step_when_the_job_is_back(self):
        """The flag, the postpone kept, then the next attempt closes it."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        # The flag came between the attempt's last check and its store.
        self._postponed()
        self.assertEqual(self.qjob.state, "pending")
        self.assertEqual(self.job.state, "cancelled")
        self.qjob.write({"state": "started"})
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(self.job.state, "cancelled")
        with self._connected(cls):
            self.assertEqual(
                self.job.execute(), abstract_executor.CANCELLED_WHILE_RUNNING,
            )
        closing = cls.instances[-1]
        self.assertEqual(closing.closed[0], "failure")
        self.assertEqual(list(closing.closed[1]), ["First"])
        self.assertFalse(Path(self.directory).exists())
        self.assertFalse(Path(cls.instances[0]._job_dir_path).exists())
        self.assertIn("trapped", self._log())
        self.assertFalse(self._row())
        self.assertEqual(self._runs(), ["First", "Apart"])

    def test_a_close_that_cannot_reach_the_server_is_picked_up_later(self):
        """The hook closes the job; the step waits for the reconciliation."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self.qjob.write({"state": "started"})
        with self._connected(cls, connect=TestLostContact._refused):
            self.job.execute()
        self.assertEqual(cls.instances[-1].closed[0], "failure")
        self.assertTrue(self._row())
        # Closed by its hook: no attempt takes it up again.
        self.assertTrue(self._row().closed)
        self.assertTrue(Path(self.directory).exists())
        # What queue_job stores of that attempt: the stop.
        self.qjob.write({
            "state": "done", "result": abstract_executor.CANCELLED_WHILE_RUNNING,
        })
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(self.job.state, "cancelled")
        self._reconcile(cls)
        self.assertFalse(Path(self.directory).exists())
        self.assertFalse(Path(cls.instances[0]._job_dir_path).exists())
        self.assertFalse(self._row())
        self.assertEqual(self.job.state, "cancelled")
        # No hook a second time.
        self.assertEqual(sum(1 for e in cls.instances if e.closed), 1)

    def test_a_server_never_reached_again_holds_its_queue(self):
        """Its step started there: past the step's cap and an hour, the row
        stays and holds the queue, with no give-up (13-nov-2026)."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self.qjob.write({"state": "started"})
        with self._connected(cls, connect=TestLostContact._refused):
            self.job.execute()
        self.qjob.write({
            "state": "done", "result": abstract_executor.CANCELLED_WHILE_RUNNING,
        })
        self._reconcile(cls, connect=TestLostContact._refused)
        self.assertTrue(self._row())
        # Its step started long ago, but the job closed now: the cap and
        # the hour count from the close.
        self._row().write({
            "step_started_at": fields.Datetime.now() - timedelta(hours=3),
        })
        self._retry_due()
        self._reconcile(cls, connect=TestLostContact._refused)
        self.assertTrue(self._row())
        self.assertFalse(any("gives up on it" in line for line in self._log()))
        self.assertFalse(self._given_up_alert())
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date - interval '3 hours'"
            " WHERE id = %s", (self.job.id,),
        )
        self._retry_due()
        self._reconcile(cls, connect=TestLostContact._refused)
        row = self._row()
        self.assertTrue(row)
        self.assertTrue(row.holds_queue_since)
        self.assertTrue(self.env["cloud.job"]._scope_busy(None, self.host.id))
        self.assertFalse(any("gives up on it" in line for line in self._log()))
        self.assertFalse(self._given_up_alert())
        # Said by the close that marked it, not at every pass.
        unreachable = [
            line for line in self._log()
            if line.startswith("The server could not be reached to stop")
        ]
        self.assertEqual(len(unreachable), 1)

    def _due_to_give_up(self, started=True):
        """Cancelled, closed without reaching its server, an hour past the cap.

        :param bool started: its step had started on the server; when not,
            the row holds no queue (cancelled between two steps)
        :return: the executor class; the next pass gives the row up when
            the step had not started
        """
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self.qjob.write({"state": "started"})
        with self._connected(cls, connect=TestLostContact._refused):
            self.job.execute()
        self.qjob.write({
            "state": "done", "result": abstract_executor.CANCELLED_WHILE_RUNNING,
        })
        self._reconcile(cls, connect=TestLostContact._refused)
        if not started:
            self._not_started()
        self._past_the_cap()
        return cls

    def _not_started(self):
        """The row as one of a job cancelled between two steps: no mark."""
        self._row().write({"detached_started": False, "holds_queue_since": False})

    def _past_the_cap(self):
        """Put the step's start and the job's close three hours back."""
        self._row().write({
            "step_started_at": fields.Datetime.now() - timedelta(hours=3),
        })
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date - interval '3 hours'"
            " WHERE id = %s", (self.job.id,),
        )
        self._retry_due()

    def test_an_alert_that_fails_lets_the_row_go(self):
        """Kept, the row would be given up again at every pass."""
        cls = self._due_to_give_up(started=False)
        with patch.object(
            type(self.env["cloud.alert"]), "raise_alert", autospec=True,
            side_effect=RuntimeError("the alert broke"),
        ) as raise_alert, mute_logger("odoo.addons.incubacloud.models.cloud_job"):
            self._reconcile(cls, connect=TestLostContact._refused)
        raise_alert.assert_called_once()
        self.assertFalse(self._row())
        self.assertTrue(any("gives up on it" in line for line in self._log()))
        self.assertFalse(self._given_up_alert())
        self.assertEqual(self.job.state, "cancelled")

    def test_the_alert_raised_again_is_the_same_one(self):
        """A pass whose commit failed gives the row up again: one alert."""
        cls = self._due_to_give_up(started=False)
        row = self._row()
        plan = row._data()["plan"]
        Job = self.env["cloud.job"]
        Job._alert_cleanup_given_up(self.job, row, plan, "unreachable")
        Job._alert_cleanup_given_up(self.job, row, plan, "unreachable")
        self.assertEqual(len(self._given_up_alert()), 1)
        self._reconcile(cls, connect=TestLostContact._refused)
        self.assertFalse(self._row())
        self.assertEqual(len(self._given_up_alert()), 1)

    def test_the_alert_says_why_and_names_a_directory_only_once_started(self):
        """A step left to end past the point of no return is not an
        unanswered server; cancelled between two steps, no directory."""
        cls = self._due_to_give_up()
        row = self._row()
        plan = row._data()["plan"]
        self.env["cloud.job"]._alert_cleanup_given_up(
            self.job, row, plan, "past_no_return",
        )
        alert = self._given_up_alert()
        self.assertIn("past the point of no return", alert.message)
        self.assertNotIn("has not answered", alert.message)
        self.assertEqual(alert.payload["directory"], self.directory)
        alert.unlink()
        row.write({"detached_started": False})
        self.env["cloud.job"]._alert_cleanup_given_up(
            self.job, row, plan, "not_removed",
        )
        alert = self._given_up_alert()
        self.assertIn("could not be removed", alert.message)
        self.assertNotIn("ctl.sh", alert.message)
        self.assertIsNone(alert.payload["directory"])
        self.assertIn(cls.instances[0]._job_dir_path, alert.message)

    def test_a_job_not_cancelled_given_up_raises_no_alert(self):
        """Its failure has its own alert."""
        cls = self._parked()
        self._postponed()
        self.qjob.unlink()
        self._reconcile(cls, connect=TestLostContact._refused)
        self.assertEqual(self.job.state, "failed")
        self.assertTrue(self._row())
        # A step that started holds its queue instead (test_detached_hold).
        self._not_started()
        self._past_the_cap()
        self._reconcile(cls, connect=TestLostContact._refused)
        self.assertFalse(self._row())
        self.assertTrue(any("gives up on it" in line for line in self._log()))
        self.assertFalse(self._given_up_alert())


class TestADeployStoppedWhileParkedInItsInitialization(_StopCase):
    """The deploy's own hooks close it in the attempt that comes back.

    «Initialize database» runs apart (phase 3, 12-nov-2026). Stopped while
    the job is parked in it, the attempt that comes back runs no
    ``before_execute``: it stops the step and closes the deploy as a
    failure, which puts its new instance back in ``draft``.
    """

    def test_the_instance_goes_back_to_draft(self):
        from ..models.deploy_instance_executor import DeployInstanceExecutor

        project = self.env["cloud.project"].create({
            "name": "parked-deploy", "remote_folder": "parkeddeploy",
        })
        instance = self.env["cloud.instance"].create({
            "name": "parked-deploy", "project_id": project.id,
            "host_id": self.host.id, "environment": "production",
            "state": "deploying",
        })
        self.job.instance_id = instance
        self.env.flush_all()
        plan = list(self._plan(apart=_TRAPPED.format(runs=self.runs)))
        plan[1] = ("Initialize database", plan[1][1], {
            "stop_on_failure": True, "detached": True,
        })
        cls = type("_ParkedDeploy", (_Parking, DeployInstanceExecutor), {
            "_job_type": None, "plan": tuple(plan), "spend_at": "follow",
            "instances": (), "prepared": 0, "busy_once": False, "spent": False,
            "on_failure": DeployInstanceExecutor.on_failure,
        })
        self.assertIsInstance(self._execute(cls), abstract_executor.JobParked)
        self.assertTrue(Path(self.directory).exists())
        self.job.cancel_job()
        self.env.flush_all()
        self._postponed()
        self.qjob.write({"state": "started"})
        self.env["cloud.job"].invalidate_model(["state"])
        with self._connected(cls):
            self.assertEqual(
                self.job.execute(), abstract_executor.CANCELLED_WHILE_RUNNING,
            )
        self.assertEqual(cls.prepared, 1)
        instance.invalidate_recordset()
        self.assertEqual(instance.state, "draft")
        self.assertEqual(instance.status, "error")
        self.assertFalse(Path(self.directory).exists())
        self.assertIn("trapped", self._log())
        self.assertFalse(self._row())
        self.assertEqual(self._runs(), ["First", "Apart"])


class TestTheReconciliationReachesTheServer(_StopCase):
    """A parked job's queue.job ended or deleted by hand: the cron closes it."""

    def test_cancelled_by_hand(self):
        """Not a job that never ran: its step is stopped, its hook runs."""
        cls = self._parked()
        self._postponed()
        # What queue_job's own Cancel button stores.
        self.qjob.write({
            "state": "cancelled", "date_cancelled": fields.Datetime.now(),
        })
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(self.job.state, "started")
        self._reconcile(cls)
        self.assertEqual(self.job.state, "cancelled")
        self.assertEqual(cls.instances[-1].closed[0], "failure")
        self.assertEqual(list(cls.instances[-1].closed[1]), ["First"])
        self.assertIn("trapped", self._log())
        self.assertFalse(Path(self.directory).exists())
        self.assertFalse(self._row())

    def test_done_by_hand(self):
        """Closed done as queue_job says, with nothing left on the server."""
        cls = self._parked()
        self._postponed()
        self.qjob.write({"state": "done"})
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(self.job.state, "started")
        self._reconcile(cls)
        self.assertEqual(self.job.state, "done")
        self.assertFalse(Path(self.directory).exists())
        self.assertFalse(Path(cls.instances[0]._job_dir_path).exists())
        self.assertFalse(self._row())

    def test_deleted_by_hand(self):
        """Closed failed, with nothing left on the server."""
        cls = self._parked()
        self._postponed()
        self.qjob.unlink()
        self._reconcile(cls)
        self.assertEqual(self.job.state, "failed")
        self.assertFalse(Path(self.directory).exists())
        self.assertFalse(self._row())

    def test_a_step_past_the_point_of_no_return_is_left_to_end(self):
        """Not stopped: removed by a later pass, once it ended on its own."""
        done = os.path.join(self.marks, "ended")
        cls = self._parked(
            apart=(
                f"echo Apart >> {self.runs}; echo line-1; sleep 5;"
                f" echo line-2; touch {done}"
            ),
            _no_return_from="Apart",
        )
        self._postponed()
        self.qjob.unlink()
        self._reconcile(cls)
        self.assertEqual(self.job.state, "failed")
        self.assertTrue(self._row())
        self.assertTrue(Path(self.directory).exists())
        for _wait in range(60):
            if Path(f"{self.directory}/exit").exists():
                break
            subprocess.run(["sleep", "0.2"], check=False)
        self.assertTrue(Path(done).exists())
        self._retry_due()
        self._reconcile(cls)
        self.assertFalse(Path(self.directory).exists())
        self.assertFalse(self._row())
        self.assertIn("line-2", self._log())


class TestARowNotResumedIsClosedFirst(_StopCase):
    """An attempt that finds a row it does not resume cleans up after it first.

    A row closed by a hook that could not reach the server, and the job
    run again by hand (queue_job's Requeue); or a row another queue job of
    the same cloud job wrote. The attempt neither resumes it nor writes
    over it: it stops what the row says runs, removes its staging, then
    starts the job afresh with a row of its own.
    """

    def _closed_unreached(self):
        """Park, stop, close without reaching the server: the row is kept."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self.qjob.write({"state": "started"})
        with self._connected(cls, connect=TestLostContact._refused):
            self.job.execute()
        self.assertTrue(self._row().closed)
        self.assertTrue(Path(self.directory).exists())
        return cls

    def _run_again(self, uuid=None):
        """The job runs again: started, by the same queue job or a new one."""
        if uuid:
            qjob = self.env["queue.job"].sudo().create({
                "uuid": uuid, "name": "again", "state": "started",
                "method_name": "execute", "model_name": "cloud.job",
                "func_string": "execute()",
            })
            self.job.sudo().write({
                "queue_job_uuid": qjob.uuid, "queue_job_id": qjob.id,
            })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'started' WHERE id = %s", (self.job.id,),
        )
        self.env.invalidate_all()

    def _assert_started_afresh(self, cls, old_dir):
        """The old step was stopped, then the whole plan ran, once."""
        self.assertFalse(Path(old_dir).exists())
        self.assertFalse(Path(cls.instances[0]._job_dir_path).exists())
        self.assertIn("trapped", self._log())
        self.assertEqual(cls.prepared, 2)
        # The last executor built is the one that cleaned up for the run.
        last = next(e for e in reversed(cls.instances) if e.closed)
        self.assertFalse(last._resumed)
        self.assertEqual(last.closed[0], "success")
        self.assertEqual(self._runs(), ["First", "Apart", "First", "Apart", "Last"])
        self.assertFalse(self._row())

    def test_a_closed_row_is_not_resumed_by_a_requeue(self):
        """Requeue after the hook: the job runs from its start."""
        cls = self._closed_unreached()
        cls.plan = self._plan(apart=f"echo Apart >> {self.runs}; echo again")
        self._run_again()
        self.assertIsNone(self._execute(cls))
        self._assert_started_afresh(cls, self.directory)

    def test_another_queue_job_s_row_is_closed_before_the_run(self):
        """A new queue job: the old one's step goes first, then the job runs."""
        cls = self._parked()
        old_dir = self.directory
        cls.plan = self._plan(apart=f"echo Apart >> {self.runs}; echo again")
        self._run_again(uuid="parking-qj-2")
        self.assertNotEqual(self._dir(1), old_dir)
        self.assertIsNone(self._execute(cls))
        self._assert_started_afresh(cls, old_dir)

    def test_a_step_that_cannot_be_stopped_defers_the_run(self):
        """Past the point of no return and alive: 30 s, the row untouched."""
        cls = self._parked(_no_return_from="Apart")
        row_data = self._row().data
        self._run_again(uuid="parking-qj-2")
        exc = self._execute(cls)
        self.assertIsInstance(exc, RetryableJobError)
        self.assertEqual(exc.seconds, 30)
        self.assertTrue(exc.ignore_retry)
        self.assertEqual(cls.prepared, 1)
        self.assertEqual(self._row().queue_job_uuid, "parking-qj")
        self.assertEqual(self._row().data, row_data)
        self.assertTrue(Path(self.directory).exists())


class TestAStoppedParkedJobEndedByHand(_StopCase):
    """Stopped while parked, then its queue.job ended by hand: a stop all the same."""

    def _stopped_then(self, end):
        """Park, stop, store the postpone, then *end* the queue.job by hand."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self._postponed()
        end()
        self.env["cloud.job"].invalidate_model(["state"])
        self.assertEqual(self.job.state, "cancelled")
        Job = type(self.env["cloud.job"])
        ends = []
        original = Job._on_state_changed

        def _recorded(records, new_state, after_commit=False):
            """Note the end acted on, and whether the checkpoint was there."""
            records.env.cr.execute(
                "SELECT 1 FROM cloud_job_checkpoint WHERE job_id = %s",
                (self.job.id,),
            )
            ends.append((records.ids, new_state, bool(records.env.cr.fetchone())))
            return original(records, new_state, after_commit=after_commit)

        with patch.object(
            Job, "_on_state_changed", autospec=True, side_effect=_recorded,
        ):
            self._reconcile(cls)
        self.assertEqual(self.job.state, "cancelled")
        closing = [e for e in cls.instances if e.closed]
        self.assertEqual(len(closing), 1)
        self.assertEqual(closing[0].closed[0], "failure")
        self.assertEqual(list(closing[0].closed[1]), ["First"])
        self.assertIn("trapped", self._log())
        self.assertFalse(Path(self.directory).exists())
        self.assertFalse(self._row())
        # The end, and the dispatch it sets off, read the row let go.
        self.assertIn(([self.job.id], "cancelled", False), ends)

    def test_cancelled_by_hand(self):
        """queue_job's own Cancel button."""
        self._stopped_then(lambda: self.qjob.write({
            "state": "cancelled", "date_cancelled": fields.Datetime.now(),
        }))

    def test_deleted_by_hand(self):
        """The queue.job deleted."""
        self._stopped_then(self.qjob.unlink)

    def _stopped_and_cancelled_by_hand(self):
        """Park, stop, store the postpone, then queue_job's Cancel button.

        :return: the executor class
        """
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self._postponed()
        self.qjob.write({
            "state": "cancelled", "date_cancelled": fields.Datetime.now(),
        })
        self.env["cloud.job"].invalidate_model(["state"])
        return cls

    def _assert_let_go(self):
        """Closed cancelled, its row gone, and no pass takes it up again."""
        self.assertEqual(self.job.state, "cancelled")
        self.assertFalse(self._row())
        self.assertEqual(
            self.env["cloud.job"]._stopping_ids([("id", "=", self.job.id)]), set(),
        )
        self._age()
        self.assertNotIn(self.job.id, self.env["cloud.job"]._lost_job_ids())

    def test_a_pass_cut_off_before_the_close_lets_the_row_go_next(self):
        """The cron died between the claim and the close: the next pass
        stops what the job left and closes it, once, letting its row go
        with the close."""
        cls = self._stopped_and_cancelled_by_hand()
        with patch.object(
            type(self.env["cloud.job"]), "_clean_up_lost_job", autospec=True,
            side_effect=RuntimeError("the cron died"),
        ), mute_logger("odoo.addons.incubacloud.models.cloud_job"):
            self._reconcile(cls)
        self.assertTrue(self.job.worker_lost_at)
        self.assertFalse(self._row().closed)
        self._reconcile(cls)
        self.assertFalse(Path(self.directory).exists())
        self._assert_let_go()
        self._reconcile(cls)
        self.assertEqual(len([
            line for line in self._log()
            if line.startswith("An earlier pass took this job up")
        ]), 1)

    def test_a_job_whose_type_has_no_executor_lets_its_row_go(self):
        """Nothing can stop what it left: its row goes, or every pass
        would take it up again; its log says what to look at."""
        self._stopped_and_cancelled_by_hand()
        self._age()
        with patch.object(
            abstract_executor.executor_registry, "get", return_value=None,
        ):
            self.env["cloud.job"]._cron_reconcile_lost_jobs()
        self.env.invalidate_all()
        self._assert_let_go()
        self.assertIn(_NO_EXECUTOR, self._log())

    @contextlib.contextmanager
    def _closes(self, held=False):
        """Note what each close of the job returns; *held*, it finds its rows held.

        Registry test mode runs every cursor in the test's transaction, so
        no other connection can hold a row of the job: held, the close's
        ``_lock_lost_job`` answers as ``SKIP LOCKED`` does on a row taken
        (``TestALockedJobIsSkipped`` takes one for real).

        :param bool held: whether the close finds the job's rows held
        :return: (yielded) what each close of the job returned, in order
        """
        Job = type(self.env["cloud.job"])
        real_close, real_lock = Job._close_lost_job, Job._lock_lost_job
        closes, closing = [], []

        def _close(model, job_id, **kwargs):
            """The real close, noted."""
            closing.append(job_id)
            try:
                closed = real_close(model, job_id, **kwargs)
            finally:
                closing.pop()
            if job_id == self.job.id:
                closes.append(closed)
            return closed

        def _lock(model, cr, job_id):
            """The real lock, but held for the job's close when told so."""
            if held and closing and job_id == self.job.id:
                return None
            return real_lock(model, cr, job_id)

        with patch.object(
            Job, "_close_lost_job", autospec=True, side_effect=_close,
        ), patch.object(
            Job, "_lock_lost_job", autospec=True, side_effect=_lock,
        ):
            yield closes

    def _chained(self):
        """A step waiting on the job in its graph.

        :return: the step's ``cloud.job``
        """
        qjob = self.env["queue.job"].sudo().create({
            "uuid": "parking-tail",
            "name": "tail",
            "state": "wait_dependencies",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        tail = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": self.job.job_type_id.id,
            "name": "Tail",
            "queue_job_uuid": qjob.uuid,
            "queue_job_id": qjob.id,
        })
        tail.with_context(ic_skip_state_hook=True).write(
            {"state": "wait_dependencies"},
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE queue_job SET graph_uuid = 'graph-parked' WHERE id IN %s",
            ((self.qjob.id, qjob.id),),
        )
        self.env.invalidate_all()
        return tail

    def _hooks(self, cls):
        """How many executors of *cls* ran a closing hook."""
        return len([e for e in cls.instances if e.closed])

    def _closed_on_the_second_pass(self, first_pass):
        """The first pass's close does not happen; the second's does.

        :param first_pass: the context the first pass runs in
        """
        cls = self._stopped_and_cancelled_by_hand()
        tail = self._chained()
        with first_pass:
            self._reconcile(cls)
        row = self._row()
        self.assertTrue(row)
        self.assertFalse(row.closed)
        self.assertEqual(self._hooks(cls), 1)
        self.assertEqual(tail.state, "wait_dependencies")
        self._age()
        self.assertIn(self.job.id, self.env["cloud.job"]._lost_job_ids())
        with self._closes() as closes:
            self._reconcile(cls)
        self.assertEqual(closes, [True])
        self.assertEqual(self._hooks(cls), 1)
        self.assertNotEqual(tail.state, "wait_dependencies")
        self._assert_let_go()

    def test_a_close_that_finds_its_rows_held_is_done_next_pass(self):
        """The row is the one mark of the job to close: kept until then."""
        self._closed_on_the_second_pass(self._closes(held=True))

    def test_a_close_that_raises_is_done_next_pass(self):
        """The same, when the close raised."""
        first = contextlib.ExitStack()
        first.enter_context(patch.object(
            type(self.env["cloud.job"]), "_close_lost_job", autospec=True,
            side_effect=RuntimeError("the close broke"),
        ))
        first.enter_context(
            mute_logger("odoo.addons.incubacloud.models.cloud_job"),
        )
        self._closed_on_the_second_pass(first)

    def test_a_hook_that_raises_leaves_the_row_to_the_close(self):
        """Neither the hook's transaction nor its failure lets the row go:
        the close deletes it, as ``_forget_checkpoint`` noted."""
        cls = self._stopped_and_cancelled_by_hand()
        Job = type(self.env["cloud.job"])
        real_close = Job._close_lost_job
        seen = []

        def _close(model, job_id, **kwargs):
            """Note the row and what step 2 left to do with it."""
            if job_id == self.job.id:
                model.env.cr.execute(
                    "SELECT closed FROM cloud_job_checkpoint WHERE job_id = %s",
                    (job_id,),
                )
                closed = [bool(row[0]) for row in model.env.cr.fetchall()]
                seen.append((closed, kwargs.get("release")))
            return real_close(model, job_id, **kwargs)

        with patch.object(
            cls, "on_cancelled_running", autospec=True,
            side_effect=RuntimeError("the hook broke"),
        ) as hook, patch.object(
            Job, "_close_lost_job", autospec=True, side_effect=_close,
        ), mute_logger("AbstractExecutor"):
            self._reconcile(cls)
        hook.assert_called_once()
        self.assertEqual(seen, [([False], "delete")])
        self._assert_let_go()

    def test_an_end_that_cannot_be_written_keeps_the_row(self):
        """The row and the end go together, or neither does."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self._postponed()
        self.qjob.unlink()
        self.env["cloud.job"].invalidate_model(["state"])
        with patch.object(
            type(self.env["cloud.job"]), "_close_lost_job_without_queue",
            autospec=True, side_effect=RuntimeError("the end broke"),
        ), mute_logger("odoo.addons.incubacloud.models.cloud_job"):
            self._reconcile(cls)
        row = self._row()
        self.assertTrue(row)
        self.assertFalse(row.closed)
        self._reconcile(cls)
        self.assertEqual(self._hooks(cls), 1)
        self._assert_let_go()

    def test_a_job_taken_up_again_whose_server_does_not_answer(self):
        """Its row is closed with its end, and later passes only try to
        clean its server: no new line, no second close."""
        cls = self._stopped_and_cancelled_by_hand()
        with patch.object(
            type(self.env["cloud.job"]), "_clean_up_lost_job", autospec=True,
            side_effect=RuntimeError("the cron died"),
        ), mute_logger("odoo.addons.incubacloud.models.cloud_job"):
            self._reconcile(cls)
        with self._closes() as closes:
            self._reconcile(cls, connect=TestLostContact._refused)
            row = self._row()
            self.assertTrue(row.closed)
            self.assertFalse(row.parked)
            self.assertEqual(self.job.state, "cancelled")
            self.assertEqual(closes, [True])
            log = self._log()
            self._retry_due()
            self._age()
            self.assertEqual(
                self.env["cloud.job"]._claim_lost_job(self.job.id),
                ("checkpoint", False),
            )
            self._reconcile(cls, connect=TestLostContact._refused)
        self.assertEqual(closes, [True, False])
        self.assertEqual(self._log(), log)
        self.assertTrue(self._row())
        self.assertEqual(self._hooks(cls), 0)

    def test_a_clean_up_that_always_raises_closes_the_job_once(self):
        """Its creator gone, say: the job is closed and its row closed."""
        cls = self._stopped_and_cancelled_by_hand()
        Job = type(self.env["cloud.job"])
        real = Job._as_its_creator

        def _gone(records):
            """The job's creator cannot be read."""
            if self.job.id in records.ids:
                raise RuntimeError("its creator is gone")
            return real(records)

        with patch.object(
            Job, "_as_its_creator", autospec=True, side_effect=_gone,
        ), self._closes() as closes, mute_logger(
            "odoo.addons.incubacloud.models.cloud_job",
        ):
            self._reconcile(cls)
            self.assertTrue(self._row().closed)
            self._reconcile(cls)
        self.assertEqual(closes, [True, False])
        self.assertEqual(self.job.state, "cancelled")
        self.assertTrue(self._row().closed)
        self.assertEqual(
            self.env["cloud.job"]._stopping_ids([("id", "=", self.job.id)]), set(),
        )

    def test_no_executor_says_so_and_alerts_once(self):
        """Its log, the server's log and one alert; the row goes with the
        close."""
        self._stopped_and_cancelled_by_hand()
        logger = "odoo.addons.incubacloud.models.cloud_job"
        with self._closes(held=True), self.assertLogs(logger, "WARNING") as logs:
            self._reconcile(None)
        self.assertTrue(any(
            f"cloud.job {self.job.id}: its type host_probe has no executor"
            in line for line in logs.output
        ))
        self.assertTrue(self._row())
        self.assertIn(_NO_EXECUTOR, self._log())
        alert = self._given_up_alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("has no executor in this server any more", alert.message)
        self.assertEqual(alert.payload["directory"], self.directory)
        with self._closes() as closes:
            self._reconcile(None)
        self.assertEqual(closes, [True])
        self.assertEqual(len(self._given_up_alert()), 1)
        self.assertEqual(self._log().count(_NO_EXECUTOR), 1)
        self._assert_let_go()

    def test_no_executor_for_a_job_not_cancelled_raises_no_alert(self):
        """Its failure has its own alert."""
        self._parked()
        self._postponed()
        self.qjob.unlink()
        with mute_logger("odoo.addons.incubacloud.models.cloud_job"):
            self._reconcile(None)
        self.assertEqual(self.job.state, "failed")
        self.assertFalse(self._row())
        self.assertIn(_NO_EXECUTOR, self._log())
        self.assertFalse(self._given_up_alert())

    def test_no_executor_for_an_ended_job_lets_its_row_go_at_once(self):
        """Nothing to close: its row goes in the clean-up."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self.qjob.write({"state": "started"})
        with self._connected(cls, connect=TestLostContact._refused):
            self.job.execute()
        self.qjob.write({
            "state": "done", "result": abstract_executor.CANCELLED_WHILE_RUNNING,
        })
        self.assertTrue(self._row().closed)
        with self._closes(held=True), mute_logger(
            "odoo.addons.incubacloud.models.cloud_job",
        ):
            self._reconcile(None)
        self.assertFalse(self._row())
        self.assertIn(_NO_EXECUTOR, self._log())
        self.assertEqual(len(self._given_up_alert()), 1)

    def test_a_close_that_never_happens_lets_the_row_go_after_30_minutes(self):
        """Closed after the pass's close, with an error: no longer a job
        to close."""
        cls = self._stopped_and_cancelled_by_hand()
        with self._closes(held=True):
            self._reconcile(cls)
        self.assertFalse(self._row().closed)
        self.env.cr.execute(
            "UPDATE cloud_job SET worker_lost_at = worker_lost_at"
            " - interval '31 minutes' WHERE id = %s", (self.job.id,),
        )
        logger = "odoo.addons.incubacloud.models.cloud_job"
        with self._closes(held=True) as closes, self.assertLogs(
            logger, "ERROR",
        ) as logs:
            self._reconcile(cls)
        self.assertEqual(closes, [False])
        self.assertTrue(any(
            "could not be closed in 30 minutes" in line for line in logs.output
        ))
        row = self._row()
        self.assertTrue(row.closed)
        self.assertFalse(row.parked)
        self._age()
        self.assertEqual(
            self.env["cloud.job"]._claim_lost_job(self.job.id),
            ("checkpoint", False),
        )
        self.assertEqual(self._hooks(cls), 1)

    def test_a_job_back_after_a_long_pause_is_closed_before_it_is_let_go(self):
        """No pass for more than 30 minutes after its claim (the panel
        stopped, the cron off in a deploy): the pass that comes back
        closes it, and its row goes with the close."""
        cls = self._stopped_and_cancelled_by_hand()
        with patch.object(
            type(self.env["cloud.job"]), "_clean_up_lost_job", autospec=True,
            side_effect=RuntimeError("the cron died"),
        ), mute_logger("odoo.addons.incubacloud.models.cloud_job"):
            self._reconcile(cls)
        self.assertTrue(self.job.worker_lost_at)
        self.env.cr.execute(
            "UPDATE cloud_job SET worker_lost_at = worker_lost_at"
            " - interval '31 minutes' WHERE id = %s", (self.job.id,),
        )
        with self._closes() as closes:
            self._reconcile(cls)
        self.assertEqual(closes, [True])
        self.assertFalse(Path(self.directory).exists())
        self._assert_let_go()

    def test_the_row_holds_the_host_until_the_close(self):
        """A build on the host waits for the close, not for the clean-up."""
        cls = self._stopped_and_cancelled_by_hand()
        self._row().write({"build_lock_host_id": self.host.id})
        self.env.flush_all()
        other = self._other_build()
        with self.assertRaises(RetryableJobError):
            other.pre_run_checks()
        with self._closes(held=True):
            self._reconcile(cls)
        with self.assertRaises(RetryableJobError):
            other.pre_run_checks()
        self._reconcile(cls)
        other.pre_run_checks()  # the job closed: must not raise

    def _other_build(self):
        """A rebuild of an instance of the job's host.

        :return: its executor
        """
        project = self.env["cloud.project"].create({"name": "parked-host-build"})
        instance = self.env["cloud.instance"].create({
            "name": "parked-host-build",
            "project_id": project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        JobType = self.env["cloud.job.type"]
        job_type = JobType.search(
            [("code", "=", "rebuild_instance")], limit=1,
        ) or JobType.create({
            "name": "rebuild_instance",
            "code": "rebuild_instance",
            "apply_to": "instance",
        })
        job = self.env["cloud.job"].create({
            "host_id": self.host.id,
            "instance_id": instance.id,
            "job_type_id": job_type.id,
            "name": "Rebuild Instance",
        })
        return RebuildInstanceExecutor(job, self.host)


class TestTheReconciliationServesClosesFirst(_StopCase):
    """Rows of servers it cannot reach do not hold up the jobs to close."""

    def _left_unreached(self):
        """A job closed whose server could not be reached: its row stays."""
        cls = self._parked()
        self.job.cancel_job()
        self.env.flush_all()
        self.qjob.write({"state": "started"})
        with self._connected(cls, connect=TestLostContact._refused):
            self.job.execute()
        self.qjob.write({
            "state": "done", "result": abstract_executor.CANCELLED_WHILE_RUNNING,
        })
        self._age()
        return cls

    def _lost_active_job(self):
        """Another job, active, whose queue.job is gone: one to close."""
        other = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": self.job.job_type_id.id,
            "name": "Lost",
            "queue_job_uuid": "lost-qj",
        })
        other.with_context(ic_skip_state_hook=True).write({"state": "started"})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date - interval '10 minutes'"
            " WHERE id = %s", (other.id,),
        )
        return other

    def test_rows_to_clean_come_after_jobs_to_close(self):
        """Whatever their ids."""
        self._left_unreached()
        other = self._lost_active_job()
        self.assertLess(self.job.id, other.id)
        ids = self.env["cloud.job"]._lost_job_ids()
        self.assertEqual(ids, [other.id, self.job.id])

    def test_a_row_tried_is_tried_again_ten_minutes_later(self):
        """Its last try is kept; until then, the pass leaves it."""
        cls = self._left_unreached()
        self._reconcile(cls, connect=TestLostContact._refused)
        row = self._row()
        self.assertTrue(row.reconcile_tried_at)
        self._age()
        self.assertNotIn(self.job.id, self.env["cloud.job"]._lost_job_ids())
        self._retry_due()
        self.assertIn(self.job.id, self.env["cloud.job"]._lost_job_ids())

    def test_a_row_to_clean_starts_no_clean_up_past_the_budget(self):
        """Not even the first of a pass: it does not count as one to close."""
        cls = self._left_unreached()
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_RECONCILE_HOOK_BUDGET_S", 0), patch.object(
            Job, "_clean_up_lost_job", autospec=True, return_value=(False, None),
        ) as clean_up:
            self._reconcile(cls)
        clean_up.assert_not_called()
        self.assertTrue(self._row())


class TestADirectoryLeftByAnEndedStep(_StopCase):
    """A step that ended but left a process in its session keeps its directory.

    ``clean`` refuses it (75); the step's result stands, and the
    directory's index is kept in the row, for whoever reaches the server
    next.
    """

    def test_it_is_removed_once_its_process_ended(self):
        """The reconciliation removes it, and only then the row."""
        lingering = os.path.join(self.marks, "lingering")
        cls = self._class(self._plan(apart=(
            f"echo Apart >> {self.runs}; echo line-1;"
            f" (sleep 2; touch {lingering}) >/dev/null 2>&1 &"
        )))
        self.assertIsNone(self._execute(cls))
        self.assertEqual(cls.instances[0].closed[0], "success")
        row = self._row()
        self.assertTrue(row.closed)
        self.assertEqual(row._data().get("unclean"), [1])
        self.assertTrue(Path(self._dir(1)).exists())
        self.qjob.write({"state": "done"})
        for _wait in range(50):
            if Path(lingering).exists():
                break
            subprocess.run(["sleep", "0.2"], check=False)
        subprocess.run(["sleep", "0.3"], check=False)
        self._reconcile(cls)
        self.assertFalse(Path(self._dir(1)).exists())
        self.assertFalse(self._row())


class TestAStepApartSays(TransactionCase):
    """What names a step's directory."""

    def test_the_queue_job_names_the_directory_too(self):
        """Two queue jobs of one cloud job never share a step's directory."""
        self.assertNotEqual(
            detached_dir(self.env, 7, "qj-a", 1),
            detached_dir(self.env, 7, "qj-b", 1),
        )
        self.assertEqual(
            detached_dir(self.env, 7, "qj-a", 1),
            detached_dir(self.env, 7, "qj-a", 1),
        )
