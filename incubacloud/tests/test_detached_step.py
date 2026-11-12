"""A step run apart on its server answers as an attached one, and is closed.

A step marked ``detached`` runs on the server in a session of its own
(``scripts/detached_step.sh``), so it can outlive the request that runs the
job: on Odoo.sh a request is cut at 900 s, and a build takes longer. Until
a job can leave such a step running between two attempts, it follows it
to its end within the attempt — and must get from it what
``transport.execute`` would have given: the same lines in the job log, the
same exit status and stdout, the same timeout, the same stop when a person
cancels the job.

The steps run for real, on this machine, through ``bash``: the transport
is a mock of ``SSHTransport`` whose commands run locally. Registry test
mode: the durable cursors ride on the test's transaction.
"""
import asyncio
import contextlib
import inspect
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.queue_job.exception import RetryableJobError

from ..models import abstract_executor
from ..models.abstract_executor import (
    DETACHED_BUSY_RETRY_S,
    DETACHED_EXIT_BUSY,
    DETACHED_EXIT_KILLED_AT_LOGOUT,
    _LOG_OFFSET,
    AbstractExecutor,
    JobCancelled,
    detached_dir,
)
from ..models.cloud_host import CloudHost
from ..models.registry import executor_registry
from ..models.transport import CommandResult, SSHTransport

#: Output with an empty line, trailing blanks, stderr and a failing exit.
_STEP = "printf 'one\\n\\ntwo  \\n'; echo warn >&2; exit 3"


async def _local_run(command, timeout=3600):
    """Run *command* with ``bash`` here, as ``SSHTransport.run`` does there.

    :return: its stdout, stripped, and its exit status
    :rtype: CommandResult
    """
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        raise
    return CommandResult(stdout=out.decode().strip(), exit_status=proc.returncode)


async def _local_execute(command, on_stdout, on_stderr, timeout=3600):
    """Run *command* here, streaming its lines, as ``SSHTransport.execute``.

    :return: its non-empty stdout lines, without trailing blanks, and its
        exit status
    :rtype: CommandResult
    """
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    kept = []

    async def _pump(stream, handler, keep):
        """Hand each non-empty line of *stream* to *handler*."""
        async for raw in stream:
            line = raw.decode().rstrip()
            if line:
                await handler(line)
                if keep:
                    kept.append(line)

    try:
        await asyncio.wait_for(asyncio.gather(
            _pump(proc.stdout, on_stdout, True),
            _pump(proc.stderr, on_stderr, False),
            proc.wait(),
        ), timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        raise
    return CommandResult(stdout="\n".join(kept), exit_status=proc.returncode)


async def _local_upload(files, mode=None):
    """Write *files* here, as ``SSHTransport.upload_text_files`` does there."""
    for path, text in files.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(path, mode)


def _local_transport(start_answer=None):
    """Return a transport whose commands run on this machine.

    :param start_answer: a ``CommandResult`` the server gives to
        ``detached_step.sh start`` instead of running it
    """
    transport = MagicMock(spec=SSHTransport)

    async def _run(command, timeout=3600):
        """Answer a start as told, run anything else."""
        if start_answer is not None and "detached_step.sh start" in command:
            return start_answer
        return await _local_run(command, timeout)

    transport.run.side_effect = _run
    transport.execute.side_effect = _local_execute
    transport.upload_text_files.side_effect = _local_upload
    return transport


class _Apart(AbstractExecutor):
    """Runs the test's plan, and records the hook that closed it.

    No ``_job_type``, so it never enters the executor registry.
    """

    plan = ()
    closed = None

    def get_commands(self):
        """The test's plan."""
        return list(self.plan)

    async def on_success(self, results):
        """Record the success."""
        self.closed = ("success", results)

    async def on_failure(self, results, errors):
        """Record the failure and what had run."""
        self.closed = ("failure", results)


class TestStepsApart(TransactionCase):
    """A job of a host whose steps run on this machine."""

    def setUp(self):
        """The host, its job, and a place for what the steps leave."""
        super().setUp()
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        self.host = self.env["cloud.host"].create({
            "name": "apart-host",
            "ip_address": "192.0.2.94",
            "user": "odoo",
            "wildcard_domain": "apart.example.com",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1,
        )
        qjob = self.env["queue.job"].sudo().create({
            "uuid": "apart-qj",
            "name": "apart",
            "state": "started",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        self.job = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": job_type.id,
            "name": "Apart",
            "queue_job_uuid": qjob.uuid,
            "queue_job_id": qjob.id,
        })
        self.env.flush_all()
        self.marks = tempfile.mkdtemp()
        self.addCleanup(self._clean_server)

    def _clean_server(self):
        """Stop and remove what a failed test may have left running."""
        for index in range(4):
            directory = self._dir(index)
            if Path(f"{directory}/ctl.sh").exists():
                subprocess.run(
                    ["bash", f"{directory}/ctl.sh", "stop", directory, "0"],
                    check=False, capture_output=True,
                )
            subprocess.run(["rm", "-rf", directory], check=False)
        subprocess.run(["rm", "-rf", self.marks], check=False)
        # A step's processes, orphaned, are children of the first process
        # here when nothing else reaps them: their zombies are reaped now.
        with contextlib.suppress(ChildProcessError):
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass

    def _dir(self, index):
        """The directory step *index* of the job runs apart in."""
        return detached_dir(self.env, self.job.id, self.job.queue_job_uuid, index)

    def _executor(self, *plan):
        """Return an executor of the job that runs *plan*."""
        executor = _Apart(self.job, self.host, sleep_interval=0.05)
        executor.plan = plan
        return executor

    def _steps(self, executor, transport=None):
        """Run the executor's plan through its step loop, on this machine.

        The job directory is staged as ``_async_entry`` stages it, and
        removed afterwards.
        """
        transport = transport or _local_transport()

        async def _go():
            try:
                return await executor._run_steps(transport, list(executor.plan))
            finally:
                await executor._discard_staging(transport)

        return asyncio.run(_go())

    def _run(self, executor, transport=None):
        """Run the executor as a job runs it, connected to this machine."""
        transport = transport or _local_transport()

        @contextlib.asynccontextmanager
        async def _connect(_host):
            yield transport

        with patch.object(
            CloudHost, "get_transport", autospec=True, side_effect=_connect,
        ):
            executor.run()

    def _raised(self, exc_type, func, *args):
        """Return what *func* raised, which must be *exc_type*.

        Not ``assertRaises``: it rolls back what the block wrote.
        """
        try:
            func(*args)
        except exc_type as exc:
            return exc
        self.fail(f"{exc_type.__name__} not raised")

    def _log(self):
        """The job's log, one line per chunk."""
        return self.env["cloud.job.log.chunk"].search(
            [("job_id", "=", self.job.id)], order="id",
        ).mapped("content")

    def _row(self):
        """The job's checkpoint, if any."""
        return self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", self.job.id)],
        )

    def test_a_step_apart_answers_as_an_attached_one(self):
        """Same exit status, same stdout, same lines in the log."""
        executor = self._executor(
            ("Attached", _STEP),
            ("Apart", _STEP, {"detached": True}),
        )
        results, skipped = self._steps(executor)
        self.assertEqual(skipped, [])
        self.assertEqual(results["Apart"], results["Attached"])
        self.assertEqual(results["Apart"], {"stdout": "one\ntwo", "exit_status": 3})
        executor._flush_logs()
        log = self._log()
        for line in ("one", "two", "warn"):
            self.assertEqual(log.count(line), 2, line)
        self.assertFalse(Path(self._dir(1)).exists())
        row = self._row()
        self.assertEqual(row.next_index, 2)
        self.assertFalse(row.detached_started)
        self.assertEqual(row._data()["results"], results)
        self.assertEqual(row._data()["plan"][1], ["Apart", _STEP, {"detached": True}])

    def test_the_offset_is_written_with_the_lines_it_covers(self):
        """One flush writes the lines and how far into the step's log they go."""
        executor = self._executor(("Apart", "true", {"detached": True}))
        executor._write_checkpoint({"next_index": 0})
        executor._checkpoint = True
        executor._log_buffer.extend([("a", "stdout"), ("b", "stdout"), (4, _LOG_OFFSET)])
        self.assertEqual(executor._flush_logs(), 2)
        self.assertEqual(self._log(), ["a", "b"])
        self.assertEqual(self._row().log_offset, 4)
        # Past the log's cap the lines go, and the offset is still written.
        executor._cap_marker_emitted = True
        executor._log_buffer.extend([("c", "stdout"), (6, _LOG_OFFSET)])
        self.assertEqual(executor._flush_logs(), 0)
        self._row().invalidate_recordset()
        self.assertEqual(self._row().log_offset, 6)
        self.assertEqual(self._log(), ["a", "b"])

    def test_the_cap_cuts_a_step_apart_as_it_cuts_an_attached_one(self):
        """``timeout`` on the server; the job sees ``asyncio.TimeoutError``."""
        executor = self._executor(
            ("Apart", "echo started; sleep 30", {"detached": True, "timeout": 1}),
        )
        self._raised(asyncio.TimeoutError, self._steps, executor)
        executor._flush_logs()
        log = self._log()
        self.assertIn("started", log)
        self.assertIn("⚠ Command exceeded 1s timeout — killed.", log)
        self.assertFalse(Path(self._dir(0)).exists())
        self.assertFalse(executor._detached_left_alive)

    def test_a_step_whose_process_died_fails_with_a_clear_message(self):
        """No exit status and no process: the server lost it."""
        executor = self._executor(("Apart", (
            "echo before; sid=$(ps -o sid= -p $$ | tr -d ' ');"
            " pkill -KILL -s \"$sid\""
        ), {"detached": True}))
        exc = self._raised(RuntimeError, self._steps, executor)
        self.assertIn("neither ended nor runs", str(exc))
        executor._flush_logs()
        self.assertIn("before", self._log())
        self.assertFalse(Path(self._dir(0)).exists())

    def test_a_server_that_kills_at_logout_runs_the_step_attached(self):
        """78: the step runs in the request, as before, and says so."""
        transport = _local_transport(start_answer=CommandResult(
            stdout="[incubacloud] ERROR: this server kills odoo's processes"
                   " when its SSH session ends",
            exit_status=DETACHED_EXIT_KILLED_AT_LOGOUT,
        ))
        executor = self._executor(("Apart", _STEP, {"detached": True}))
        results, _skipped = self._steps(executor, transport)
        self.assertEqual(results["Apart"], {"stdout": "one\ntwo", "exit_status": 3})
        transport.execute.assert_called_once()
        self.assertEqual(transport.execute.call_args.args[0], _STEP)
        self.assertTrue(executor._detached_attached)
        executor._flush_logs()
        self.assertTrue(any(
            "this server kills odoo's processes" in line
            and "runs attached" in line
            for line in self._log()
        ))

    def test_a_server_without_a_tool_runs_the_step_attached(self):
        """79: as with 78, the step runs in the request and the log says why."""
        transport = _local_transport(start_answer=CommandResult(
            stdout="[incubacloud] ERROR: this server lacks pgrep pkill:"
                   " the step cannot run apart",
            exit_status=79,  # DETACHED_EXIT_MISSING_TOOLS
        ))
        executor = self._executor(("Apart", _STEP, {"detached": True}))
        results, _skipped = self._steps(executor, transport)
        self.assertEqual(results["Apart"], {"stdout": "one\ntwo", "exit_status": 3})
        transport.execute.assert_called_once()
        self.assertTrue(executor._detached_attached)
        executor._flush_logs()
        self.assertTrue(any(
            "lacks pgrep pkill" in line and "runs attached" in line
            for line in self._log()
        ))

    def test_a_start_that_may_have_started_is_stopped_and_removed(self):
        """Any answer of start but 73, 74 and 78: the step may run there."""
        transport = _local_transport()

        async def _ambiguous(command, timeout=3600):
            """The step starts, and start answers as if it had not."""
            result = await _local_run(command, timeout)
            if "detached_step.sh start" in command:
                return CommandResult(
                    stdout="[incubacloud] ERROR: the step did not start within 10 s",
                    exit_status=1,
                )
            return result

        transport.run.side_effect = _ambiguous
        executor = self._executor(
            (
                "Apart", "trap 'echo trapped; exit 7' TERM; sleep 300 & wait",
                {"detached": True},
            ),
        )
        exc = self._raised(RuntimeError, self._steps, executor, transport)
        self.assertIn("did not start within 10 s", str(exc))
        self.assertFalse(Path(self._dir(0)).exists())
        self.assertFalse(executor._detached_left_alive)

    def test_another_job_s_live_step_makes_the_job_try_again(self):
        """73: the job parks at that step, and keeps its place."""
        transport = _local_transport(start_answer=CommandResult(
            stdout="[incubacloud] ERROR: another step still runs for host"
                   " on this server: job 12 Build",
            exit_status=DETACHED_EXIT_BUSY,
        ))
        executor = self._executor(
            ("First", "echo first"),
            ("Apart", "echo apart", {"detached": True}),
        )
        exc = self._raised(RetryableJobError, self._run, executor, transport)
        self.assertEqual(exc.seconds, DETACHED_BUSY_RETRY_S)
        self.assertTrue(exc.ignore_retry)
        self.assertTrue(self._row().parked)
        self.assertEqual(self._row().next_index, 1)
        self.assertTrue(any("job 12 Build" in line for line in self._log()))

    def test_the_hook_that_closes_the_job_takes_its_row(self):
        """A job that ended leaves nothing on the server and no place kept."""
        executor = self._executor(
            ("First", "echo first"),
            ("Apart", "echo apart", {"detached": True}),
        )
        self._run(executor)
        self.assertEqual(executor.closed[0], "success")
        self.assertEqual(executor.closed[1]["Apart"], {"stdout": "apart", "exit_status": 0})
        self.assertFalse(self._row())
        self.env.cr.execute(
            "SELECT count(*) FROM cloud_job_outcome WHERE job_id = %s", (self.job.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (1,))

    def test_a_failed_job_s_hook_takes_its_row_too(self):
        """``on_failure`` closes the job, and the row goes with it."""
        executor = self._executor(("Apart", "exit 4", {"detached": True}))
        self._raised(RuntimeError, self._run, executor)
        self.assertEqual(executor.closed[0], "failure")
        self.assertFalse(self._row())

    def test_a_step_apart_cancelled_is_stopped_then_closed(self):
        """Window 1: TERM to the step, its trap's last words, then the hook."""
        mark = os.path.join(self.marks, "started")
        executor = self._executor(
            ("First", "echo first"),
            ("Apart", (
                "trap 'echo trapped; exit 7' TERM; echo going;"
                f" touch {shlex.quote(mark)}; sleep 300 & wait"
            ), {"detached": True}),
        )

        def _stop_once_started(_self):
            """The person's stop, once the step runs on the server."""
            if Path(mark).exists():
                raise JobCancelled(abstract_executor.CANCELLED_WHILE_RUNNING)

        with patch.object(
            _Apart, "_check_cancel", autospec=True, side_effect=_stop_once_started,
        ):
            self._raised(JobCancelled, self._run, executor)
        directory = self._dir(1)
        self.assertFalse(Path(directory).exists())
        self.assertEqual(executor.closed[0], "failure")
        self.assertEqual(list(executor.closed[1]), ["First"])
        log = self._log()
        self.assertIn("going", log)
        self.assertIn("trapped", log)
        self.assertFalse(self._row())
        self.assertFalse(executor._detached_left_alive)


class TestAPlanWithoutStepsApart(BaseCase):
    """An executor with no step marked ``detached`` touches none of it."""

    def test_no_row_and_no_job_read(self):
        """Its loop runs without a job: nothing reads or writes a checkpoint."""

        class _Plain(AbstractExecutor):
            """Plain steps only."""

            def get_commands(self):
                """Unused: the test hands the loop its steps."""
                return []

        executor = object.__new__(_Plain)
        executor._log_buffer = []
        transport = MagicMock(spec=SSHTransport)
        transport.execute.return_value = CommandResult("", 0)
        results, _skipped = asyncio.run(executor._run_steps(
            transport, [("A", "a"), ("B", "b", {"timeout": 5})],
        ))
        self.assertEqual(list(results), ["A", "B"])
        self.assertFalse(executor._checkpoint)
        transport.run.assert_not_called()

    def test_only_the_deploy_and_the_rebuild_mark_steps(self):
        """Steps are marked one by one, each with its own review and QA.

        The first, the rebuild's image build (decided on 10-nov-2026),
        marked in core's rebuild and inherited by every rebuild built on
        it; then the deploy's and the rebuild's long steps (phase 3,
        12-nov-2026), inherited the same way. No other module of an
        executor marks one.
        """
        marked = re.compile(r"""["']detached["']\s*:""")
        found = sorted({
            inspect.getmodule(cls).__name__.rsplit(".", 1)[-1]
            for cls in executor_registry.all().values()
            if marked.search(inspect.getsource(inspect.getmodule(cls)))
        })
        self.assertEqual(
            found, ["deploy_instance_executor", "rebuild_instance_executor"],
        )

    def test_every_class_adds_what_it_carries(self):
        """A mixin's ``_carried`` adds to its bases' instead of hiding them."""

        class _Base(AbstractExecutor):
            """Carries one attribute."""

            _carried = ("_a",)
            _a = 1

            def get_commands(self):
                """Unused."""
                return []

        class _Mixin:
            """Carries another."""

            _carried = ("_b",)
            _b = 2

        class _Both(_Mixin, _Base):
            """Both."""

        executor = object.__new__(_Both)
        executor._chunks_persisted = 0
        executor._cap_marker_emitted = False
        self.assertEqual(
            executor._carried_names(),
            ("_chunks_persisted", "_cap_marker_emitted", "_b", "_a"),
        )
        self.assertEqual(executor._carried_state()["_a"], 1)
