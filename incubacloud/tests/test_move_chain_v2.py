"""The move between hosts that freezes, verifies, measures and may wait.

``test_cloud_instance_move`` pins the chain's order; here each new step
does what it promises: the deploy builds no database, the quiet check
postpones a busy instance without stopping it, the fingerprint and the
restore compare what crossed, the stop and the cutover measure the cut,
the old copy is frozen and removed later, the rollback hook hears why
the move did not happen, a moving instance counts on both hosts, and a
priority weighs a stack against its neighbours.
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tools.misc import file_path

from ..models.abstract_executor import JobRefused
from ..models.deploy_instance_executor import DeployInstanceExecutor, _half_memory
from ..models.move_steps_executor import parse_fingerprint
from ..models.registry import executor_registry
from ..models.restore_instance_executor import RestoreInstanceExecutor
from ..models.transport import SSHTransport
from ._template import pin_template
from .test_executor_features import _make_deploy_executor

_SKIPPED = (
    "Start database", "Initialize database",
    "Initialize module checksums", "Set system parameters",
)


class _MoveCase(TransactionCase):
    """A deployed instance on one host, a second host to move it to."""

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "v2-proj"})
        hosts = [
            self.env["cloud.host"].create({
                "name": name, "ip_address": ip, "port": 22, "user": "root",
                "login_type": "ssh_key", "wildcard_domain": f"{name}.example.com",
                "status": "compatible", "traefik_deployed": True,
            })
            for name, ip in (("v2-src", "192.0.2.71"), ("v2-tgt", "192.0.2.72"),
                             ("v2-third", "192.0.2.73"))
        ]
        self.source, self.target, self.third = hosts
        self.inst = self.env["cloud.instance"].create({
            "name": "v2-movable", "project_id": self.project.id,
            "environment": "production", "host_id": self.source.id,
            "state": "deployed",
        })
        self.Job = self.env["cloud.job"]

    def _job(self, code, host, payload=None, state=None, inst=None):
        """Create a *code* job on *host* for the instance, as the queue would."""
        job = self.Job.create({
            "name": code, "host_id": host.id,
            "instance_id": (inst or self.inst).id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            ).id,
            "payload": payload or False,
        })
        if state:
            job.write({"state": state})
        return job

    def _executor(self, job):
        """The executor the runner builds for *job*."""
        return executor_registry.get(job.job_type_id.code)(job, job.host_id)

    def _chain(self, **options):
        """Move the instance to the target; return the chain's jobs, in order."""
        self.inst.move_to_host(self.target, move_options=options or None)
        return self.inst._get_move_chain_jobs()

    def _step(self, chain, code):
        """The first step of *chain* of type *code*."""
        return chain.filtered(lambda j: j.job_type_id.code == code)[:1]

    def _running(self, chain, code):
        """Make *chain* look dispatched, with its *code* step running.

        :return: that step
        """
        chain.write({"state": "wait_dependencies"})
        step = self._step(chain, code)
        step.write({"state": "started"})
        return step


class TestDeployForAMove(TransactionCase):
    """The destination is built without a database of its own."""

    def test_a_moves_deploy_builds_no_database(self):
        labels = [
            c[0] for c in
            _make_deploy_executor(move_origin_host_id=7).get_commands()
        ]
        for label in _SKIPPED:
            self.assertNotIn(label, labels)
        self.assertIn("Pull images", labels)
        self.assertIn("Start instance", labels)

    def test_a_deploy_outside_a_move_keeps_every_step(self):
        labels = [c[0] for c in _make_deploy_executor().get_commands()]
        for label in (*_SKIPPED, "Pull images", "Start instance"):
            self.assertIn(label, labels)


class TestQuietCheck(_MoveCase):

    def _postponed_outcome(self):
        """What the postponement's cleanup will tell the hook, or None."""
        cleanup = self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "move_rollback_cleanup"),
        ], order="id desc", limit=1)
        return (cleanup.payload or {}).get("move_outcome")

    def test_a_job_of_the_panel_running_postpones_the_move(self):
        chain = self._chain()
        check = self._running(chain, "move_quiet_check")
        other = self._job("rebuild_instance", self.source, state="started")
        asyncio.run(self._executor(check).on_success({}))
        # Nothing on its own row: its hook's cursor commits before the
        # job's transaction stores it done (test_move_own_job_row).
        self.assertFalse(check.result)
        # What waited of the chain is gone; the check itself goes on.
        rest = chain - check
        self.assertEqual(set(rest.mapped("state")), {"cancelled"})
        self.assertNotEqual(check.state, "cancelled")
        cleanup = self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "move_rollback_cleanup"),
        ])
        self.assertEqual(cleanup.host_id, self.target)
        self.assertTrue(cleanup.payload["move_postponed"])
        self.assertEqual(cleanup.payload["move_outcome"], {
            "kind": "postponed", "reason": "busy",
            "detail": "1 jobs running", "job_id": check.id,
            "trigger": "manual",
        })
        # The source never stopped: nothing starts it, nothing alerts.
        self.assertFalse(self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "start_instance"),
        ]))
        self.assertTrue(self.inst.move_rollback_in_progress)
        self.assertFalse(self.env["cloud.alert"].search([
            ("instance_id", "=", self.inst.id),
            ("code", "in", ("move_stuck", "job_failed")),
        ]))
        self.assertNotIn(other, chain)

    def test_the_watchdog_leaves_a_postponed_move_to_its_cleanup(self):
        chain = self._chain()
        check = self._running(chain, "move_quiet_check")
        other = self._job("rebuild_instance", self.source, state="started")
        asyncio.run(self._executor(check).on_success({}))
        # Only the cleanup is left to run.
        (other | check).write({"state": "done"})
        self.env["cloud.instance"].cron_recover_stuck_moves()
        self.assertTrue(self.inst.move_origin_host_id)
        self.assertFalse(self.env["cloud.alert"].search([
            ("instance_id", "=", self.inst.id), ("code", "=", "move_stuck"),
        ]))

    def test_a_postponed_moves_cleanup_tells_the_hook_why(self):
        chain = self._chain()
        check = self._running(chain, "move_quiet_check")
        self._job("rebuild_instance", self.source, state="started")
        asyncio.run(self._executor(check).on_success({}))
        cleanup = self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "move_rollback_cleanup"),
        ])
        with patch.object(
            type(self.inst), "_on_move_rolled_back", autospec=True,
        ) as hook:
            asyncio.run(self._executor(cleanup).on_success({}))
        hook.assert_called_once()
        self.assertEqual(hook.call_args.kwargs["target_host"], self.target)
        self.assertEqual(hook.call_args.kwargs["outcome"]["kind"], "postponed")
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertFalse(self.inst.move_rollback_in_progress)
        self.assertEqual(self.inst.host_id, self.source)

    def test_a_quiet_instance_lets_the_move_go_on(self):
        chain = self._chain()
        check = self._running(chain, "move_quiet_check")
        asyncio.run(self._executor(check).on_success({}))
        self.assertFalse(check.result)
        self.assertFalse(self.inst.move_rollback_in_progress)
        self.assertNotIn("cancelled", (chain - check).mapped("state"))

    def test_a_layers_count_inside_the_instance_postpones_too(self):
        chain = self._chain()
        check = self._running(chain, "move_quiet_check")
        results = {
            "Count what runs inside the instance": {
                "stdout": "2\n", "exit_status": 0,
            },
        }
        asyncio.run(self._executor(check).on_success(results))
        self.assertEqual(self._postponed_outcome()["detail"], "2 jobs running")

    def test_a_count_that_could_not_be_read_postpones_without_failing(self):
        """The source has not stopped: no failure, no watchdog, no pause."""
        chain = self._chain(trigger="window")
        check = self._running(chain, "move_quiet_check")
        executor = self._executor(check)
        for results in (
            {"Count what runs inside the instance": {
                "stdout": "", "exit_status": 1,
            }},
            {"Count what runs inside the instance": {
                "stdout": "ERROR: relation does not exist\n", "exit_status": 0,
            }},
        ):
            with self.subTest(results=results):
                self.assertEqual(executor.parse_results(results), [])
                self.assertEqual(executor._verdict(results)[0], "unknown")
        asyncio.run(executor.on_success({
            "Count what runs inside the instance": {
                "stdout": "", "exit_status": 1,
            },
        }))
        outcome = self._postponed_outcome()
        self.assertEqual(outcome["reason"], "unknown")
        self.assertEqual(outcome["trigger"], "window")
        self.assertIn("status 1", outcome["detail"])
        self.assertTrue(self.inst.move_rollback_in_progress)
        self.assertFalse(self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "start_instance"),
        ]))

    def test_the_count_inside_runs_only_when_a_layer_gives_its_sql(self):
        chain = self._chain()
        check = self._running(chain, "move_quiet_check")
        self.assertEqual(self._executor(check).get_commands(), [])
        with patch.object(
            type(self.inst), "_move_quiet_sql", autospec=True,
            return_value="SELECT 0;",
        ):
            (step,) = self._executor(check).get_commands()
        self.assertIn("move_steps.sh", step[1])
        self.assertIn("quiet", step[1])

    def test_requests_postpone_through_the_layers_answer(self):
        chain = self._chain(trigger="idle")
        check = self._running(chain, "move_quiet_check")
        with patch.object(
            type(self.inst), "_move_requests_check", autospec=True,
            return_value=("active", "3 requests in 2 min"),
        ) as asked:
            asyncio.run(self._executor(check).on_success({}))
        asked.assert_called_once_with(self.inst, "idle")
        self.assertEqual(self._postponed_outcome()["reason"], "active")
        self.assertEqual(self._postponed_outcome()["trigger"], "idle")


class TestRollbackOutcome(_MoveCase):
    """The hook hears why, read before the markers that name the chain go."""

    def _hooked(self):
        return patch.object(
            type(self.inst), "_on_move_rolled_back", autospec=True,
        )

    def _run_cleanup(self):
        cleanup = self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "move_rollback_cleanup"),
        ], order="id desc", limit=1)
        with self._hooked() as hook:
            asyncio.run(self._executor(cleanup).on_success({}))
        return hook.call_args.kwargs["outcome"]

    def _dispatched(self, chain):
        """Make *chain* look dispatched, its steps no longer in the queue."""
        chain.write({"state": "wait_dependencies"})

    def test_a_failed_step_found_by_the_watchdog(self):
        chain = self._chain()
        self._dispatched(chain)
        deploy = chain[0]
        deploy.write({"state": "failed"})
        (chain - deploy).write({"state": "cancelled"})
        self.env["cloud.instance"].cron_recover_stuck_moves()
        outcome = self._run_cleanup()
        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(outcome["job_id"], deploy.id)
        self.assertEqual(outcome["job_type"], deploy.job_type_id.code)

    def test_a_chain_that_died_without_a_failure_is_stuck(self):
        chain = self._chain()
        self._dispatched(chain)
        chain.write({"state": "cancelled"})
        self.env["cloud.instance"].cron_recover_stuck_moves()
        self.assertEqual(self._run_cleanup()["kind"], "stuck")

    def test_an_operators_rollback(self):
        chain = self._chain()
        self._dispatched(chain)
        self.inst.rollback_move()
        self.assertEqual(self._run_cleanup()["kind"], "manual")

    def test_a_chain_cancelled_in_the_queue(self):
        chain = self._chain()
        chain.write({"state": "queued"})
        with self._hooked() as hook:
            chain[0].cancel_job()
        self.assertEqual(hook.call_args.kwargs["outcome"]["kind"], "cancelled")


class TestFingerprint(_MoveCase):

    def test_the_fingerprint_is_kept_on_its_job(self):
        chain = self._chain()
        job = self._step(chain, "move_fingerprint")
        out = '{"tables": {"res_users": 3, "ir_attachment": 9}, "files": 12}\n'
        executor = self._executor(job)
        results = {"Count rows and files": {"stdout": out, "exit_status": 0}}
        self.assertEqual(executor.parse_results(results), [])
        asyncio.run(executor.on_success(results))
        self.assertEqual(job.result, {
            "tables": {"res_users": 3, "ir_attachment": 9}, "files": 12,
        })

    def test_it_counts_the_tables_the_layers_name(self):
        chain = self._chain()
        (step,) = self._executor(self._step(chain, "move_fingerprint")).get_commands()
        for table in self.inst._move_fingerprint_tables():
            self.assertIn(table, step[1])
        self.assertIn("fingerprint", step[1])

    def test_a_count_that_printed_nothing_usable_fails(self):
        chain = self._chain()
        executor = self._executor(self._step(chain, "move_fingerprint"))
        for stdout in ("", "not json", '{"tables": [], "files": 1}'):
            with self.subTest(stdout=stdout):
                self.assertTrue(executor.parse_results({
                    "Count rows and files": {"stdout": stdout, "exit_status": 0},
                }))
        self.assertIsNone(parse_fingerprint('{"tables": {"t": "x"}, "files": 1}'))

    def test_the_script_skips_a_table_the_database_lacks(self):
        with open(file_path("incubacloud/scripts/move_steps.sh")) as handle:
            self.assertIn("to_regclass", handle.read())

    def test_a_fingerprint_once_the_move_is_over_is_refused(self):
        chain = self._chain()
        job = self._step(chain, "move_fingerprint")
        self.inst.write({"host_id": self.target.id})
        with self.assertRaises(JobRefused):
            self._executor(job).pre_run_checks()


class TestRestoreVerifies(_MoveCase):

    def setUp(self):
        super().setUp()
        self.inst.move_origin_host_id = self.source
        self.inst.move_target_host_id = self.target
        key = "v2-chain"
        self.fingerprint = self._job("move_fingerprint", self.source)
        self.download = self._job("backup_download", self.source)
        (self.fingerprint | self.download).write({"queue_chain_key": key})
        self.fingerprint.result = {
            "tables": {"res_users": 3, "ir_config_parameter": 40}, "files": 5,
        }

    def _restore(self, **payload):
        job = self._job("restore_instance", self.target, payload={
            "mode": "from_host", "source_job_id": self.download.id,
        } | payload)
        job.queue_chain_key = "v2-chain"
        return self._executor(job)

    def _commands(self, executor):
        Restore = RestoreInstanceExecutor
        with patch.object(
            Restore, "_remote_path", autospec=True, return_value="/tmp/r.zip",
        ), patch.object(
            Restore, "_local_handoff_dir", autospec=True, return_value="",
        ):
            return executor.get_commands()

    def test_the_check_runs_between_the_restore_and_odoos_start(self):
        executor = self._restore(verify_job=self.fingerprint.id)
        executor.pre_run_checks()
        labels = [c[0] for c in self._commands(executor)]
        verify = labels.index("Verify integrity")
        self.assertEqual(labels[verify - 1], "Restore database")
        self.assertLess(verify, labels.index("Start Odoo service"))
        command = self._commands(executor)[verify][1]
        for part in ("verify", "5", "ir_config_parameter=40", "res_users=3"):
            self.assertIn(part, command)

    def test_a_difference_fails_before_odoo_starts(self):
        executor = self._restore(verify_job=self.fingerprint.id)
        commands = self._commands(executor)
        mismatch = "Integrity check failed: res_users source 3, target 2\n"

        async def run(command, on_stdout, on_stderr, timeout=None):
            """Answer each command, the integrity check with a difference."""
            if "move_steps.sh" in command and "verify" in command:
                return SimpleNamespace(exit_status=1, stdout=mismatch)
            return SimpleNamespace(exit_status=0, stdout="")

        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = run
        results, skipped = asyncio.run(executor._run_steps(transport, commands))
        self.assertIn("Start Odoo service", skipped)
        self.assertNotIn("Start Odoo service", results)
        errors = executor._outcome_errors(results, skipped)
        self.assertEqual(
            errors, ["Integrity check failed: res_users source 3, target 2"],
        )

    def test_a_restore_outside_a_move_checks_nothing(self):
        labels = [c[0] for c in self._commands(self._restore())]
        self.assertNotIn("Verify integrity", labels)

    def test_a_restore_without_its_fingerprint_is_refused(self):
        self.fingerprint.result = {}
        with self.assertRaises(JobRefused):
            self._restore(verify_job=self.fingerprint.id).pre_run_checks()

    def test_a_fingerprint_of_another_chain_is_not_taken(self):
        self.fingerprint.queue_chain_key = "another"
        with self.assertRaises(JobRefused):
            self._restore(verify_job=self.fingerprint.id).pre_run_checks()


class TestTheCutIsMeasured(_MoveCase):

    def test_the_sources_stop_stamps_the_start_of_the_cut(self):
        self.registry_enter_test_mode()
        stop = self._job("stop_instance", self.source, payload={
            "services": ["odoo"], "move_cut": True,
        })
        asyncio.run(self._executor(stop).before_execute(MagicMock(spec=SSHTransport)))
        self.inst.invalidate_recordset(["move_cut_started_at"])
        self.assertTrue(self.inst.move_cut_started_at)

    def test_another_stop_stamps_nothing(self):
        self.registry_enter_test_mode()
        stop = self._job("stop_instance", self.source, payload={
            "services": ["odoo"], "move_target": True,
        })
        asyncio.run(self._executor(stop).before_execute(MagicMock(spec=SSHTransport)))
        self.inst.invalidate_recordset(["move_cut_started_at"])
        self.assertFalse(self.inst.move_cut_started_at)

    def _cut_over(self, seconds):
        """Cut over a move whose cut started *seconds* ago; return the hook."""
        self.inst.write({
            "host_id": self.source.id,
            "move_origin_host_id": self.source.id,
            "move_target_host_id": self.target.id,
            "move_cut_started_at": fields.Datetime.now() - timedelta(seconds=seconds),
        })
        cutover = self._job("move_cutover", self.target)
        with patch.object(
            type(self.inst), "_on_move_cutover", autospec=True,
        ) as hook:
            asyncio.run(self._executor(cutover).on_success({}))
        return hook

    def _slow_cut_alert(self):
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.inst.id), ("code", "=", "move_slow_cut"),
            ("state", "=", "active"),
        ])

    def test_the_cutover_keeps_the_cut_and_passes_it_on(self):
        hook = self._cut_over(30)
        self.assertAlmostEqual(self.inst.move_last_cut_seconds, 30, delta=5)
        self.assertFalse(self.inst.move_cut_started_at)
        self.assertEqual(hook.call_args.args[1], self.source)
        self.assertAlmostEqual(hook.call_args.kwargs["cut_seconds"], 30, delta=5)

    def test_a_slow_cut_alerts_until_one_is_under_budget(self):
        self.env["cloud.settings"].sudo()._get().move_cut_alert_seconds = 20
        self._cut_over(30)
        self.assertEqual(len(self._slow_cut_alert()), 1)
        self.assertEqual(self._slow_cut_alert().level, "warning")
        self._cut_over(5)
        self.assertFalse(self._slow_cut_alert())


class TestTheOldCopyIsFrozen(_MoveCase):

    def setUp(self):
        super().setUp()
        # Cut over: the instance lives on the target now.
        self.inst.host_id = self.target

    def test_the_freeze_stops_the_stack_and_keeps_it_for_a_while(self):
        freeze = self._job("move_freeze_source", self.source)
        executor = self._executor(freeze)
        executor.pre_run_checks()
        (step,) = executor.get_commands()
        self.assertIn("freeze", step[1])
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.inst.move_frozen_source_host_id, self.source)
        expected = fields.Datetime.now() + timedelta(hours=48)
        self.assertAlmostEqual(
            self.inst.move_frozen_until, expected, delta=timedelta(minutes=1),
        )

    def test_the_freeze_never_stops_the_live_copy(self):
        with self.assertRaises(JobRefused):
            self._executor(self._job("move_freeze_source", self.target)).pre_run_checks()

    def _frozen(self, until_hours=-1, host=None):
        self.inst.write({
            "move_frozen_source_host_id": (host or self.source).id,
            "move_frozen_until": fields.Datetime.now() + timedelta(hours=until_hours),
        })

    def _cleanup_jobs(self):
        return self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "move_cleanup_source"),
        ])

    def test_the_cron_removes_a_copy_whose_time_is_up(self):
        self._frozen()
        self.inst.active = False
        self.assertEqual(self.env["cloud.instance"].cron_clean_frozen_move_sources(), 1)
        self.assertEqual(self._cleanup_jobs().host_id, self.source)

    def test_the_cron_leaves_a_copy_whose_time_is_not_up(self):
        self._frozen(until_hours=5)
        self.env["cloud.instance"].cron_clean_frozen_move_sources()
        self.assertFalse(self._cleanup_jobs())

    def test_the_cron_forgets_a_copy_on_a_retired_host(self):
        self._frozen()
        self.source.active = False
        self.env["cloud.instance"].cron_clean_frozen_move_sources()
        self.assertFalse(self._cleanup_jobs())
        self.assertFalse(self.inst.move_frozen_source_host_id)
        note = self.env["cloud.audit.log"].search([
            ("action", "=", "Frozen copy forgotten"),
            ("instance_id", "=", self.inst.id),
        ])
        self.assertIn(f"Frozen copy on host {self.source.name} forgotten", note.details)

    def test_the_cleanup_removes_an_archived_instances_copy(self):
        self._frozen()
        self.inst.write({"active": False, "removal_finalized": True})
        executor = self._executor(self._job("move_cleanup_source", self.source))
        labels = [c[0] for c in executor.get_commands()]
        self.assertIn("Remove instance directory", labels)
        asyncio.run(executor.on_success({}))
        self.assertFalse(self.inst.move_frozen_source_host_id)
        self.assertFalse(self.inst.move_frozen_until)

    def test_a_new_move_removes_the_copy_first(self):
        """Back to the host of the frozen copy, or to any other: the old
        copy goes before the chain, in the instance's queue."""
        for target in (self.source, self.third):
            with self.subTest(target=target.name):
                self.inst.write({
                    "host_id": self.target.id, "move_origin_host_id": False,
                    "move_target_host_id": False, "move_chain_job_ids": False,
                })
                self._frozen(until_hours=5, host=self.source)
                self.inst.move_to_host(target)
                chain = self.inst._get_move_chain_jobs()
                cleanup = self._cleanup_jobs().filtered(
                    lambda j: j.state not in ("done", "cancelled"),
                )[-1:]
                self.assertEqual(cleanup.host_id, self.source)
                self.assertLess(cleanup.id, min(chain.ids))
                self.assertFalse(cleanup.queue_chain_key)
                (chain | cleanup).write({"state": "cancelled"})

    def test_the_deploy_refuses_a_destination_that_still_holds_the_copy(self):
        """A failed removal must not let ``compose up`` start the old copy."""
        self.inst.write({
            "move_origin_host_id": self.target.id,
            "move_target_host_id": self.source.id,
        })
        self._frozen(until_hours=5, host=self.source)
        deploy = self._job("deploy_instance", self.source)
        with self.assertRaises(JobRefused) as refused:
            self._executor(deploy).pre_run_checks()
        self.assertIn("froze", str(refused.exception))
        # Removed: the move's deploy goes on there.
        self.inst.write({
            "move_frozen_source_host_id": False, "move_frozen_until": False,
        })
        self._executor(deploy).pre_run_checks()

    def test_the_deploy_leaves_a_copy_frozen_elsewhere_alone(self):
        self.inst.write({
            "move_origin_host_id": self.target.id,
            "move_target_host_id": self.third.id,
        })
        self._frozen(until_hours=5, host=self.source)
        self._executor(self._job("deploy_instance", self.third)).pre_run_checks()

    def test_a_freeze_that_failed_still_records_the_copy_and_alerts(self):
        """Possibly running, and removed by the clean-up all the same."""
        freeze = self._job("move_freeze_source", self.source)
        asyncio.run(self._executor(freeze).on_failure({}, ["exit 1"]))
        self.assertEqual(self.inst.move_frozen_source_host_id, self.source)
        self.assertTrue(self.inst.move_frozen_until)
        alert = self.env["cloud.alert"].search([
            ("instance_id", "=", self.inst.id),
            ("code", "=", "move_freeze_failed"),
            ("state", "=", "active"),
        ])
        self.assertEqual(alert.level, "warning")
        self.assertIn(self.source.name, alert.message)
        # Removed later: the alert goes with it.
        cleanup = self._job("move_cleanup_source", self.source)
        asyncio.run(self._executor(cleanup).on_success({}))
        self.assertFalse(self.inst.move_frozen_source_host_id)
        self.assertEqual(alert.state, "dismissed")

    def test_the_instances_delete_removes_the_copy_first(self):
        self._frozen(until_hours=5)
        delete_id = self.Job.enqueue(self.target.id, self.inst.id, "delete_instance")
        cleanup = self._cleanup_jobs()
        self.assertEqual(cleanup.host_id, self.source)
        self.assertLess(cleanup.id, delete_id)

    def test_the_operators_button(self):
        self._frozen(until_hours=5)
        result = self.inst.action_clean_frozen_copy()
        self.assertEqual(result["job_id"], self._cleanup_jobs().id)

    def test_deleting_the_record_forgets_the_copy_with_a_note(self):
        draft = self.env["cloud.instance"].create({
            "name": "v2-draft", "project_id": self.project.id,
            "host_id": self.target.id,
        })
        draft.write({"move_frozen_source_host_id": self.source.id})
        draft.unlink()
        self.assertTrue(self.env["cloud.audit.log"].search([
            ("action", "=", "Frozen copy forgotten"),
            ("host_id", "=", self.source.id),
        ]))


class TestCapacityDuringAMove(_MoveCase):

    def test_a_moving_instance_counts_on_both_hosts(self):
        self.inst.write({"odoo_cpus": 1.5, "db_cpus": 0.5, "backup_cpus": 0,
                         "smtp_cpus": 0})
        before = self.target.allocated_cpus
        self.inst.move_target_host_id = self.target
        self.assertAlmostEqual(self.target.allocated_cpus, before + 2.0)
        self.assertAlmostEqual(self.source.allocated_cpus, 2.0)


class TestSchedulingPriority(TransactionCase):

    def setUp(self):
        super().setUp()
        pin_template(self.env)
        project = self.env["cloud.project"].create({"name": "prio-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "prio-host", "ip_address": "192.0.2.75", "user": "ubuntu",
            "wildcard_domain": "prio.example.com",
        })
        self.inst = self.env["cloud.instance"].create({
            "name": "prioinst", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "odoo_memory_limit": "1280m", "db_memory_limit": "1g",
        })

    def _services(self):
        job = self.env["cloud.job"].create({
            "name": "prio deploy", "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "deploy_instance")], limit=1,
            ).id,
        })
        executor = DeployInstanceExecutor(job, self.host)
        return yaml.safe_load(executor._resource_override_content())["services"]

    def test_normal_is_dockers_default_weight_and_nothing_else(self):
        services = self._services()
        for svc in ("odoo", "db"):
            self.assertEqual(services[svc]["cpu_shares"], 1024)
            self.assertNotIn("mem_reservation", services[svc])
        for svc, entry in services.items():
            if svc not in ("odoo", "db"):
                self.assertNotIn("cpu_shares", entry)

    def test_each_priority_weighs_its_stack(self):
        for priority, shares in (("low", 512), ("high", 2048)):
            with self.subTest(priority=priority):
                self.inst.scheduling_priority = priority
                services = self._services()
                self.assertEqual(services["odoo"]["cpu_shares"], shares)
                self.assertEqual(services["db"]["cpu_shares"], shares)

    def test_only_high_reserves_half_the_memory(self):
        self.inst.scheduling_priority = "low"
        self.assertNotIn("mem_reservation", self._services()["odoo"])
        self.inst.scheduling_priority = "high"
        services = self._services()
        self.assertEqual(services["odoo"]["mem_reservation"], "640m")
        self.assertEqual(services["db"]["mem_reservation"], "512m")

    def test_half_of_a_memory_value(self):
        for limit, half in (("1280m", "640m"), ("2g", "1g"), ("1g", "512m"),
                            ("896m", "448m"), ("", ""), ("lots", "")):
            with self.subTest(limit=limit):
                self.assertEqual(_half_memory(limit), half)
