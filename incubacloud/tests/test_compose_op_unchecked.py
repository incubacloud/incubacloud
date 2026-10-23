"""A compose operation that ran but could not check what follows it.

``scripts/compose_op.sh`` exits ``COMPOSE_OP_EXIT_UNCHECKED`` when the
containers did what it was asked, but what the steps after it need — the
compose, what runs, odoo's routes — could not be read: the egress
sidecar restarted after odoo, odoo restarted into the gatekeeper's
network. The compose read failed once in QA (the pull request preview
walk 6f6, 2026-10-22). That is not a failed
job: the step counts as done, the job log says what was not checked, and
everything a success brings happens as usual. The health probe catches
what may be left wrong: an unfiltered route through ``odoo_net_setup``'s
healthcheck, an odoo outside its gatekeeper's network, or with its
gatekeeper stopped, through the routes odoo has.

Each executor that runs the script to start containers is driven through
its real steps, its own step loop and its outcome dispatch, with the
host answering that exit to the compose step.
"""
import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase, TransactionCase

from ..models.abstract_executor import COMPOSE_OP_EXIT_UNCHECKED
from ..models.backup_restore_executor import BackupRestoreExecutor
from ..models.restart_instance_executor import RestartInstanceExecutor
from ..models.restore_instance_executor import RestoreInstanceExecutor
from ..models.start_instance_executor import StartInstanceExecutor
from ..models.transport import CommandResult, SSHTransport

_WARNING = "the network steps that follow it could not be checked"


class _UncheckedCase(TransactionCase):
    """An instance in 'error', and a way to run a job's steps to the end."""

    def setUp(self):
        """Create the instance the jobs run on, not running and in 'error'."""
        # The outcome hooks write on a cursor of their own; without test
        # mode it cannot see this transaction's records.
        self.registry_enter_test_mode()
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "cu-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "cu-host", "ip_address": "10.0.9.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "cu.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "cu-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed",
        })
        self.instance.write({"status": "error", "running": False})

    def _executor(self, cls, code, payload=None):
        """Return *cls* built on a new ``cloud.job`` of type *code*.

        :param type cls: the executor class
        :param str code: the job type's code
        :param dict payload: the job's payload
        :return: the executor, ready to run its steps
        """
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        job = self.env["cloud.job"].create({
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": job_type.id,
            "name": f"{code} unchecked test",
        })
        job.payload = payload or {}
        # The outcome hooks read and write on their own cursor: what this
        # transaction still holds in cache would otherwise be flushed over
        # what they wrote, at the first read after them.
        self.env.flush_all()
        return cls(job, self.host)

    def _run(self, ex, exits):
        """Run *ex*'s real steps and dispatch their outcome.

        The host is an ``SSHTransport`` mock answering each command with
        the exit *exits* gives its label (0 when absent). A failed
        outcome is caught by hand: Odoo's ``assertRaises`` would roll
        back what ``on_failure`` wrote.

        :param ex: executor from :meth:`_executor`
        :param dict exits: ``{label: exit_status}`` for the non-zero steps
        :return: the error the dispatch raised, or None
        """
        commands = list(ex.get_commands())
        labels = {step[0] for step in commands}
        self.assertFalse(
            set(exits) - labels, f"no such step; the steps are {sorted(labels)}",
        )
        exit_by_command = {step[1]: exits.get(step[0], 0) for step in commands}
        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = (
            lambda command, *_handlers: CommandResult("", exit_by_command[command])
        )
        results, skipped = asyncio.run(ex._run_steps(transport, commands))
        try:
            asyncio.run(ex._dispatch_outcome(results, skipped=skipped))
        except RuntimeError as exc:
            return exc
        return None

    def _log(self, ex):
        """Return the system lines *ex* wrote, joined."""
        return "\n".join(msg for msg, source in ex._log_buffer if source == "system")

    def _assert_done_with_warning(self, ex, label, error):
        """The job succeeded, said what it did not check, and set the instance up."""
        self.assertIsNone(error)
        log = self._log(ex)
        self.assertIn(f"'{label}' did its work", log)
        self.assertIn(_WARNING, log)
        self.assertIn("the warnings above say why", log)
        self.assertIn("which this step accepts", log)
        self.assertNotIn("✗", log)
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.status, "ok")
        self.assertTrue(self.instance.running)


class TestRestoreTakesTheUncheckedStartForDone(_UncheckedCase):

    def _restore(self):
        """A restore of an uploaded archive, as a clone or a refresh runs it."""
        return self._executor(
            RestoreInstanceExecutor, "restore_instance", {"mode": "from_job"},
        )

    def test_the_restore_succeeds_with_a_warning_and_its_hooks_run(self):
        """Odoo is up on the restored database: done, warned, hooked."""
        ex = self._restore()
        with patch.object(
            type(self.instance), "_after_restore_hook", autospec=True,
        ) as hook:
            error = self._run(
                ex, {"Start Odoo service": COMPOSE_OP_EXIT_UNCHECKED},
            )
        self._assert_done_with_warning(ex, "Start Odoo service", error)
        self.assertIn("✓ Database restored successfully.", self._log(ex))
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[0].id, self.instance.id)
        self.assertEqual(hook.call_args.args[1], "restore")

    def test_the_steps_after_the_start_still_run(self):
        """The archive is still removed from the host."""
        ex = self._restore()
        with patch.object(
            type(self.instance), "_after_restore_hook", autospec=True,
        ):
            self._run(ex, {"Start Odoo service": COMPOSE_OP_EXIT_UNCHECKED})
        self.assertIn("Running: Remove remote backup file", self._log(ex))

    def test_a_start_that_failed_still_fails_the_restore(self):
        """Any other exit of the start is a failed restore, as before."""
        ex = self._restore()
        error = self._run(ex, {"Start Odoo service": 1})
        self.assertIn("'Start Odoo service' exited with status 1", str(error))
        self.assertNotIn(_WARNING, self._log(ex))
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.status, "error")

    def test_the_same_exit_from_another_step_is_still_a_failure(self):
        """Only the compose step speaks that code."""
        ex = self._restore()
        error = self._run(ex, {"Restore database": COMPOSE_OP_EXIT_UNCHECKED})
        self.assertIn("'Restore database' exited with status 24", str(error))
        self.assertNotIn(_WARNING, self._log(ex))
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.status, "error")


class TestBackupRestoreTakesTheUncheckedStartForDone(_UncheckedCase):

    def _restore(self):
        """A restore of one of the instance's own backups."""
        return self._executor(
            BackupRestoreExecutor, "backup_restore",
            {"time": "2026-10-01T02:00:00"},
        )

    def test_the_restore_succeeds_with_a_warning_and_its_hooks_run(self):
        """The stack is up on the restored database: done, warned, hooked."""
        ex = self._restore()
        with patch.object(
            type(self.instance), "_after_restore_hook", autospec=True,
        ) as hook:
            error = self._run(
                ex, {"Start instance": COMPOSE_OP_EXIT_UNCHECKED},
            )
        self._assert_done_with_warning(ex, "Start instance", error)
        self.assertIn("✓ Database restored successfully from backup.", self._log(ex))
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[1], "backup_restore")

    def test_a_start_that_failed_still_fails_the_restore(self):
        """Any other exit of ``up`` is a failed restore, as before."""
        ex = self._restore()
        error = self._run(ex, {"Start instance": 1})
        self.assertIn("'Start instance' exited with status 1", str(error))
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.status, "error")


class TestStartAndRestartTakeTheUncheckedOperationForDone(_UncheckedCase):

    def test_a_start_succeeds_with_a_warning(self):
        """The instance is started and running, with the warning."""
        ex = self._executor(StartInstanceExecutor, "start_instance")
        error = self._run(ex, {"Start containers": COMPOSE_OP_EXIT_UNCHECKED})
        self._assert_done_with_warning(ex, "Start containers", error)
        self.assertIn("✓ Instance 'cu-inst' started.", self._log(ex))

    def test_a_start_that_failed_still_fails(self):
        """Any other exit of the start fails it, as before."""
        ex = self._executor(StartInstanceExecutor, "start_instance")
        error = self._run(ex, {"Start containers": 1})
        self.assertIn("'Start containers' exited with status 1", str(error))
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.status, "error")

    def test_a_restart_succeeds_with_a_warning(self):
        """The instance is restarted and running, with the warning."""
        ex = self._executor(RestartInstanceExecutor, "restart_instance")
        error = self._run(ex, {"Restart containers": COMPOSE_OP_EXIT_UNCHECKED})
        self._assert_done_with_warning(ex, "Restart containers", error)
        self.assertIn("✓ Instance 'cu-inst' restarted.", self._log(ex))

    def test_a_restart_that_failed_still_fails(self):
        """Any other exit of the restart fails it, as before."""
        ex = self._executor(RestartInstanceExecutor, "restart_instance")
        error = self._run(ex, {"Restart containers": 1})
        self.assertIn("'Restart containers' exited with status 1", str(error))
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.status, "error")


class TestTheScriptAndTheExecutorsAgreeOnTheCode(BaseCase):

    def test_compose_op_exits_with_the_code_the_executors_accept(self):
        """A code changed on one side only would fail every such start."""
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "compose_op.sh"
        ).read_text()
        match = re.search(r"^EXIT_UNCHECKED=(\d+)$", script, re.MULTILINE)
        self.assertIsNotNone(match, "compose_op.sh no longer sets EXIT_UNCHECKED")
        self.assertEqual(int(match.group(1)), COMPOSE_OP_EXIT_UNCHECKED)
