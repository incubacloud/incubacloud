"""A rebuild left unapplied: the mark on the server and who honours it (#96).

``rebuild.sh copier-update`` lays ``.incubacloud-unapplied`` before copier
and the rebuild's restart lifts it (``tests/shell/rebuild.bats``). Start
and Restart bring back the previous build over it, or refuse when they
cannot (``tests/shell/compose_op.bats``). Here: what the panel does with
it. The rebuild hands the script its job and kind and lifts the mark in
its restart's own command; Start and Restart take the previous build for
done and a refusal for a failure that leaves the instance's status; the
restores refuse before anything runs.
"""
import asyncio
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from odoo.tests.common import BaseCase, TransactionCase

from ..models.abstract_executor import (
    COMPOSE_OP_EXIT_PREVIOUS_BUILD,
    COMPOSE_OP_EXIT_UNAPPLIED,
    JobRefused,
    UnappliedRebuild,
    parse_unapplied_mark,
)
from ..models.registry import executor_registry
from ..models.transport import CommandResult, SSHTransport
from ..restore_staging import new_upload_path
from .test_executor_features import _make_rebuild_executor

#: What ``rebuild.sh check-unapplied`` prints over a rebuild's mark.
_BUILD_MARK = "IC_UNAPPLIED job=400 kind=build\n"
#: And over a plan change's.
_FILES_MARK = "IC_UNAPPLIED job=401 kind=files\n"


def _host_answering(mark_stdout, mark_status):
    """A transport whose server answers the mark's question so, the rest well.

    :param str mark_stdout: what ``check-unapplied`` prints
    :param int mark_status: its exit status
    :return: ``MagicMock(spec=SSHTransport)``
    """
    async def _run(command, *_args, **_kwargs):
        """``check-unapplied`` as given; any other command succeeds."""
        if "rebuild.sh check-unapplied" in command:
            return CommandResult(mark_stdout, mark_status)
        return CommandResult("", 0)

    transport = MagicMock(spec=SSHTransport)
    transport.run = AsyncMock(side_effect=_run)
    transport.upload_text_files = AsyncMock()
    transport.upload_file = AsyncMock()
    transport.execute = AsyncMock()
    return transport


class TestTheRebuildLaysAndLiftsItsMark(BaseCase):

    def test_copier_update_is_handed_the_job_and_build(self):
        cmds = dict(c[:2] for c in _make_rebuild_executor().get_commands())
        argv = shlex.split(cmds["Update with copier"])
        self.assertEqual(argv[2], "copier-update")
        self.assertEqual(argv[-2:], ["42", "build"])

    def test_a_flow_that_builds_no_image_says_files(self):
        ex = _make_rebuild_executor()
        ex._builds_image = False
        cmds = dict(c[:2] for c in ex.get_commands())
        self.assertEqual(shlex.split(cmds["Update with copier"])[-1], "files")

    def test_the_restart_lifts_its_own_mark_once_it_worked(self):
        cmds = _make_rebuild_executor().get_commands()
        restart = dict(c[:2] for c in cmds)["Restart instance"]
        up, clear = restart.rsplit(" && ", 1)
        self.assertIn("docker compose up -d --remove-orphans", up)
        self.assertEqual(
            shlex.split(clear)[2:], ["clear-unapplied", "~/projects/test-inst", "42"],
        )
        # No step of its own, which a failed restart would not stop.
        self.assertEqual(
            [c[0] for c in cmds if "clear-unapplied" in c[1]], ["Restart instance"],
        )

    def test_the_mark_is_read_from_its_line(self):
        self.assertEqual(
            parse_unapplied_mark("[incubacloud] x\n" + _BUILD_MARK),
            {"job": "400", "kind": "build"},
        )
        self.assertEqual(parse_unapplied_mark(_FILES_MARK)["kind"], "files")
        self.assertIsNone(parse_unapplied_mark("nothing\n"))


class _MarkCase(TransactionCase):
    """A production instance on a host, and its jobs' executors."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "mark-host", "ip_address": "192.0.2.96", "user": "root",
            "wildcard_domain": "mark.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Mark Proj"})
        self.inst = self.env["cloud.instance"].create({
            "name": "markinst", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "status": "ok",
        })

    def _executor(self, code, payload=None, inst=None):
        """The executor the runner builds for a *code* job of the instance."""
        job = self.env["cloud.job"].create({
            "name": code, "host_id": self.host.id,
            "instance_id": (inst or self.inst).id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            ).id,
            "payload": payload or False,
        })
        return executor_registry.get(code)(job, self.host)

    @staticmethod
    def _log(executor):
        """The executor's log lines, in order."""
        return [line for line, _source in executor._log_buffer]


class TestStartOverAMark(_MarkCase):

    def test_the_previous_build_started_is_done_with_a_warning(self):
        for code, label in (
            ("start_instance", "Start containers"),
            ("restart_instance", "Restart containers"),
        ):
            with self.subTest(code=code):
                executor = self._executor(code)
                (step,) = executor.get_commands()
                self.assertIn(COMPOSE_OP_EXIT_PREVIOUS_BUILD, step[2]["continue_on"])
                results = {label: {
                    "stdout": "", "exit_status": COMPOSE_OP_EXIT_PREVIOUS_BUILD,
                }}
                self.assertEqual(executor.parse_results(results), [])
                asyncio.run(executor.on_success(results))
                self.assertEqual(self.inst.status, "ok")
                self.assertTrue(any(
                    line.startswith("⚠") and "previous build" in line
                    for line in self._log(executor)
                ))

    def test_a_refusal_fails_saying_why_and_leaves_the_status(self):
        for code, label in (
            ("start_instance", "Start containers"),
            ("restart_instance", "Restart containers"),
        ):
            with self.subTest(code=code):
                executor = self._executor(code)
                results = {label: {
                    "stdout": "", "exit_status": COMPOSE_OP_EXIT_UNAPPLIED,
                }}
                (error,) = executor.parse_results(results)
                self.assertIn("did not start anything", error)
                self.assertIn("Rebuild the instance", error)
                self.assertIn("RB-42", error)
                asyncio.run(executor.on_failure(results, [error]))
                self.assertEqual(self.inst.status, "ok")

    def test_any_other_failure_still_marks_the_instance(self):
        executor = self._executor("start_instance")
        results = {"Start containers": {"stdout": "", "exit_status": 1}}
        errors = executor.parse_results(results)
        asyncio.run(executor.on_failure(results, errors))
        self.assertEqual(self.inst.status, "error")


class TestRestoresRefuseOverAMark(_MarkCase):

    def _refused(self, executor, transport):
        """Run *executor*'s ``before_execute``; return the refusal."""
        with self.assertRaises(UnappliedRebuild) as caught:
            asyncio.run(executor.before_execute(transport))
        transport.execute.assert_not_called()
        return caught.exception

    def test_a_backup_restore_refuses_before_any_step_and_says_no_retry(self):
        executor = self._executor("backup_restore", {"time": "2026-11-12T02:00:00"})
        refusal = self._refused(executor, _host_answering(_BUILD_MARK, 26))
        self.assertIn("rebuild #400", str(refusal))
        self.assertIn("Rebuild the instance first", str(refusal))
        asyncio.run(executor.on_abort(refusal))
        log = self._log(executor)
        self.assertFalse(any("Try the restore again" in line for line in log))
        self.assertEqual(self.inst.status, "ok")

    def test_a_browser_restore_refuses_once_the_archive_is_in_the_job(self):
        """B1: the archive staged on the panel is gone with the refusal,
        and nothing of the instance was touched: the upload went into the
        job's own directory, after the payload's checks."""
        staged = new_upload_path(self.inst.id)
        Path(staged).write_bytes(b"PK\x05\x06" + b"\0" * 18)
        self.addCleanup(lambda: Path(staged).unlink(missing_ok=True))
        executor = self._executor(
            "restore_instance", {"mode": "browser", "local_path": str(staged)},
        )
        transport = _host_answering(_BUILD_MARK, 26)
        with self.assertRaises(UnappliedRebuild) as caught:
            asyncio.run(executor.before_execute(transport))
        transport.upload_file.assert_awaited_once()
        transport.execute.assert_not_called()
        self.assertFalse(Path(staged).exists())
        asyncio.run(executor.on_abort(caught.exception))
        self.assertEqual(self.inst.status, "ok")

    def test_a_plan_changes_mark_does_not_refuse(self):
        executor = self._executor("backup_restore", {"time": "2026-11-12T02:00:00"})
        asyncio.run(executor.before_execute(_host_answering(_FILES_MARK, 26)))

    def test_no_mark_does_not_refuse(self):
        executor = self._executor("backup_restore", {"time": "2026-11-12T02:00:00"})
        asyncio.run(executor.before_execute(_host_answering("", 0)))

    def test_a_question_that_failed_refuses_as_unread(self):
        executor = self._executor("backup_restore", {"time": "2026-11-12T02:00:00"})
        with self.assertRaises(JobRefused) as caught:
            asyncio.run(executor.before_execute(_host_answering("", 255)))
        self.assertNotIsInstance(caught.exception, UnappliedRebuild)
        self.assertIn("could not be read", str(caught.exception))
