"""Continuous recovery's long steps run apart from the request that runs them.

A base copy (wal-g) takes minutes or hours with a large database, and a
job follows a restore for up to ``RESTORE_WAIT_S``: on Odoo.sh a request
is cut at 900 s. «Base copy», the switch on's «First base copy» and «Wait
for the restore» run apart on the server (``detached_step.sh``) and their
jobs park around them (phase 3, cut 6, 12-nov-2026).

The base copy is a hidden job: it runs beside the visible jobs of its
instance, and its step holds a scope of its own on the server, so a
rebuild that starts meanwhile is not made to wait for it. The first base
copy holds the same scope: never two copies of an instance at once. An attempt that
takes a restore up after a park knows what the first one chose
(``_carried``), so a stop there follows the restore instead of asking a
host that may not answer. And what the steps print last, which the jobs
read, is in the tail of their stdout a step apart keeps.

The steps run for real, on this machine, as in core's ``test_detached_step``.
"""
import asyncio
import contextlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from odoo.modules.module import get_module_path

from odoo.addons.incubacloud.models.abstract_executor import (
    DETACHED_EXIT_BUSY,
    DETACHED_STDOUT_TAIL,
    JobCancelled,
    JobWorkerLost,
    detached_dir,
)
from odoo.addons.incubacloud.models.registry import executor_registry
from odoo.addons.incubacloud.tests.test_detached_step import _local_transport
from odoo.addons.incubacloud_pitr.models import pitr_executors as ex

from .test_pitr import TestCancellingARestore, _PitrCase

#: Lines that push the end of a step's stdout past the tail it keeps.
_NOISE = (
    f"for i in $(seq 1 {DETACHED_STDOUT_TAIL // 100 + 600}); do"
    " printf '%0100d\\n' \"$i\"; done"
)
_BASES = (
    'IC_PITR_BASES 77 [{"backup_name":"base_1","finish_time":"2026-10-17T02:00:00Z"},'
    '{"backup_name":"base_2","finish_time":"2026-10-18T02:00:00Z"}]'
)


def _options(commands):
    """Each step's options, by label."""
    return {item[0]: (item[2] if len(item) == 3 else {}) for item in commands}


class TestTheLongStepsRunApart(_PitrCase):
    """Which steps are marked, with which options."""

    def setUp(self):
        super().setUp()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok", "pitr_cluster_id": "77",
        })

    def test_the_base_copy_runs_apart_and_the_housekeeping_attached(self):
        base = self._executor(ex.PitrBaseBackupExecutor, "pitr_base_backup")
        self.assertEqual(_options(base.get_commands()), {
            "Base copy": {"stop_on_failure": True, "detached": True},
            "Drop what left the window": {},
        })

    def test_the_wait_runs_apart_and_the_start_attached(self):
        """In a job that starts the restore and in one that follows it."""
        executor = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00"},
        )
        executor._plan = {
            "cluster": "77", "backup": "base_000000010000000000000009",
            "timeline": "1",
        }
        self.assertEqual(_options(executor.get_commands()), {
            "Start the restore": {"stop_on_failure": True},
            "Wait for the restore": {"detached": True},
        })
        follower = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00", "follow": 5},
        )
        self.assertEqual(_options(follower.get_commands()), {
            "Wait for the restore": {"detached": True},
        })

    def test_the_first_base_copy_runs_apart_and_the_switch_attached(self):
        """The same ``base-backup`` as the daily copy, with the options it had."""
        enable = self._executor(ex.PitrEnableExecutor, "pitr_enable")
        self.assertEqual(_options(enable.get_commands()), {
            "Settle archiving": {"stop_on_failure": True},
            "Install wal-g": {"stop_on_failure": True},
            "Switch archiving on": {"stop_on_failure": True},
            "First base copy": {"detached": True},
        })

    def test_no_other_step_of_continuous_recovery_runs_apart(self):
        """Marked one by one, each with its review: these three only."""
        marked = {}
        for code in ("pitr_enable", "pitr_disable", "pitr_status"):
            executor = self._executor(executor_registry.get(code), code)
            marked[code] = [
                label for label, opts in _options(executor.get_commands()).items()
                if opts.get("detached")
            ]
        self.assertEqual(
            marked, {
                "pitr_enable": ["First base copy"], "pitr_disable": [],
                "pitr_status": [],
            },
        )


class TestABaseCopyBesideARebuild(_PitrCase):
    """The base copy's step and a rebuild's of the same instance run side by side."""

    def setUp(self):
        super().setUp()
        self.script = os.path.join(
            get_module_path("incubacloud"), "scripts", "detached_step.sh",
        )
        self.started = []
        self.addCleanup(self._clean_server)

    def _clean_server(self):
        """Stop and remove the steps the test started."""
        for directory in self.started:
            if Path(f"{directory}/ctl.sh").exists():
                subprocess.run(
                    ["bash", f"{directory}/ctl.sh", "stop", directory, "0"],
                    check=False, capture_output=True,
                )
            subprocess.run(["rm", "-rf", directory], check=False)
        with contextlib.suppress(ChildProcessError):
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass

    def _start(self, code, command):
        """Start, as the panel does, a step of a new job of type *code*.

        In the scope its executor gives it.

        :return: ``detached_step.sh start``'s exit status
        """
        executor = self._executor(executor_registry.get(code), code)
        directory = detached_dir(self.env, executor.job.id, f"pitr-{code}", 0)
        self.started.append(directory)
        return subprocess.run(
            [
                "bash", self.script, "start", directory, "60",
                executor._detached_scope(), str(executor.job.id), "Step", command,
            ],
            check=False, capture_output=True,
        ).returncode

    def test_a_rebuild_starts_while_a_base_copy_runs(self):
        """The copy holds its type's scope: the rebuild's build starts,
        and a second visible job of the instance still waits for it."""
        self.assertEqual(self._start("pitr_base_backup", "sleep 30"), 0)
        self.assertEqual(self._start("rebuild_instance", "sleep 30"), 0)
        self.assertEqual(
            self._start("restart_instance", "true"), DETACHED_EXIT_BUSY,
        )

    def test_the_first_base_copy_holds_the_daily_copys_scope(self):
        """Switching on copies beside a rebuild's step, as the daily copy
        does, but never while a daily copy of the instance runs."""
        self.assertEqual(self._start("rebuild_instance", "sleep 30"), 0)
        self.assertEqual(self._start("pitr_base_backup", "sleep 30"), 0)
        self.assertEqual(
            self._start("pitr_enable", "true"), DETACHED_EXIT_BUSY,
        )


class _RunsHere(_PitrCase):
    """A job of the instance, dispatched to queue_job, whose steps run here."""

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok", "pitr_cluster_id": "77",
        })

    def _queued(self, cls, code, payload=None):
        """An executor of a new job of type *code*, with its queue job."""
        executor = self._executor(cls, code, payload=payload)
        job = executor.job
        qjob = self.env["queue.job"].sudo().create({
            "uuid": f"pitr-apart-{job.id}", "name": code, "state": "started",
            "method_name": "execute", "model_name": "cloud.job",
            "func_string": "execute()",
        })
        job.sudo().write({"queue_job_uuid": qjob.uuid, "queue_job_id": qjob.id})
        self.env.flush_all()
        return cls(job, self.host, sleep_interval=0.05)

    def _steps(self, executor, plan):
        """Run *plan* through the executor's step loop, on this machine."""
        transport = _local_transport()

        async def _go():
            """The loop, then the staging removed as a run removes it."""
            try:
                return await executor._run_steps(transport, plan)
            finally:
                await executor._discard_staging(transport)

        return asyncio.run(_go())


class TestWhatTheStepsPrintLast(_RunsHere):
    """The lines the jobs read come last, within the tail a step apart keeps."""

    def test_the_base_copy_s_window_is_read_past_a_long_output(self):
        executor = self._queued(ex.PitrBaseBackupExecutor, "pitr_base_backup")
        results, _skipped = self._steps(executor, [(
            "Base copy", f"{_NOISE}; echo '{_BASES}'",
            {"stop_on_failure": True, "detached": True},
        )])
        stdout = results["Base copy"]["stdout"]
        # Cut to the tail: the noise alone is longer.
        self.assertLessEqual(len(stdout.encode()), DETACHED_STDOUT_TAIL)
        self.assertTrue(stdout.endswith(_BASES))
        executor._record(results)
        self.assertEqual(str(self.inst.pitr_last_base_at), "2026-10-18 02:00:00")
        self.assertEqual(str(self.inst.pitr_oldest_base_at), "2026-10-17 02:00:00")

    def test_the_restore_s_result_is_read_past_a_long_output(self):
        executor = self._queued(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00", "follow": 5},
        )
        line = (
            "IC_PITR_RESTORE job=5 state=done target=2026-10-18T22:31:00+00"
            " timeline=00000002 promoted_at=1792369056 filestore=restored"
            " cancelled=0"
        )
        results, _skipped = self._steps(executor, [(
            "Wait for the restore", f"{_NOISE}; echo '{line}'", {"detached": True},
        )])
        stdout = results["Wait for the restore"]["stdout"]
        self.assertLessEqual(len(stdout.encode()), DETACHED_STDOUT_TAIL)
        self.assertEqual(ex.parse_restore(stdout)["state"], "done")
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_success(results))
        self.assertEqual(enqueue.call_args.args[3], "pitr_base_backup")
        self.assertEqual(
            (self.inst.pitr_timelines[0]["job"], self.inst.pitr_timelines[0]["timeline"]),
            ("5", "00000002"),
        )


class TestARestoreTakenUpAfterAPark(_PitrCase):
    """An attempt that resumes a restore knows what the first one chose."""

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok", "pitr_cluster_id": "77",
        })
        for name in ("_refresh_host_disk", "_publish_bus"):
            patcher = patch.object(ex.PitrRestoreExecutor, name, autospec=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _parked(self, first):
        """The checkpoint *first* leaves, parked in its wait, as a park writes it."""
        plan = [
            [item[0], item[1], item[2] if len(item) == 3 else {}]
            for item in first.get_commands()
        ]
        return self.env["cloud.job.checkpoint"].sudo().create({
            "job_id": first.job.id,
            "host_id": self.host.id,
            "next_index": len(plan) - 1,
            "detached_started": True,
            "parked": True,
            "data": json.dumps({
                "plan": plan, "results": {}, "carried": first._carried_state(),
            }),
        })

    def _taken_up(self, first):
        """A new executor of *first*'s job that adopted its checkpoint."""
        again = ex.PitrRestoreExecutor(first.job, self.host)
        again._adopt_checkpoint(self._parked(first))
        return again

    def _abort(self, executor):
        """``on_abort`` of a stop after the park; what was asked and queued."""
        patcher, transport = TestCancellingARestore._host(self, "")
        with patcher, patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_abort(JobWorkerLost("worker died")))
        return transport, enqueue

    def test_what_the_restore_carries(self):
        """What ``on_abort`` decides with, set by ``before_execute``."""
        names = object.__new__(ex.PitrRestoreExecutor)._carried_names()
        self.assertLessEqual({"_plan", "_rejoined", "_probed"}, set(names))

    def test_a_stop_of_a_planned_restore_follows_it(self):
        """It had started the restore: the next job follows it, the host
        is not asked."""
        first = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00"},
        )
        first._plan = {
            "cluster": "77", "backup": "base_000000010000000000000009",
            "timeline": "1",
        }
        first._probed = True
        again = self._taken_up(first)
        self.assertEqual(again._plan, first._plan)
        transport, enqueue = self._abort(again)
        transport.execute.assert_not_called()
        call = enqueue.call_args
        self.assertEqual(call.args[3], "pitr_restore")
        self.assertEqual(call.kwargs["payload"]["follow"], first.job.id)

    def test_a_stop_of_a_rejoined_restore_follows_it(self):
        """The first attempt found its restore on the host and only waited."""
        first = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00"},
        )
        first._rejoined = True
        again = self._taken_up(first)
        self.assertTrue(again._rejoined)
        transport, enqueue = self._abort(again)
        transport.execute.assert_not_called()
        self.assertEqual(enqueue.call_args.kwargs["payload"]["follow"], first.job.id)

    def test_a_cancel_after_the_park_stops_the_restore_it_follows(self):
        """``restore-cancel`` of the restore the job follows, whichever
        attempt it is."""
        first = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00", "follow": 5},
        )
        again = self._taken_up(first)
        patcher, transport = TestCancellingARestore._host(
            self, "IC_PITR_RESTORE job=5 state=failed stopped_at=fetching cancelled=1",
        )
        with patcher, patch.object(self.Job, "enqueue", autospec=True):
            asyncio.run(again.on_cancelled_running(None, JobCancelled()))
        command = transport.execute.await_args.args[0]
        self.assertIn(" restore-cancel ", command)
        self.assertIn(f" 5 {ex.CANCEL_WAIT_OUTSIDE_RUN_S}", command)
