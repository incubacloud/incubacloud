"""The archive of an instance switched off goes once its window passed.

Switching off fixes the purge date (``pitr_off_at`` plus the window it
has then); the watch queues ``pitr_purge`` once it came, which empties
only what was archived before the switch off, with an environment of its
own, then the host's ``pitr.env``, and forgets the archive. Switching on
before the date keeps it. Every rebuild that writes ``pitr.env`` keeps the
prefix and destination it rendered, and says when they changed.
"""
import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from odoo import fields

from odoo.addons.incubacloud_pitr.models import pitr_executors as ex
from odoo.addons.incubacloud_pitr.models._constants import (
    ALERT_PREFIX_CHANGED,
    ALERT_PURGE_FAILED,
    ENV_FILE,
)

from .test_pitr import _PitrCase

PREFIX = "s3://bucket/backups/pp/pitr-inst-pitr"
_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "1.0.16" / "end-migrate.py"
)


def _run_migration(env):
    """Run the 1.0.16 end-migrate on *env*'s cursor, from 1.0.15."""
    spec = importlib.util.spec_from_file_location("pitr_1_0_16_end", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.migrate(env.cr, "1.0.15")


class _SwitchedOffCase(_PitrCase):

    def setUp(self):
        super().setUp()
        self.backend.s3_secret_access_key = "pitr-off-secret"
        self.inst.write({
            "pitr_enabled": False, "pitr_state": "off",
            "pitr_cluster_id": "7698",
            "pitr_previous_clusters": [{"id": "7001", "until": "2026-10-01 00:00:00"}],
        })

    def _switched_off(self, days_ago, purge_in=None):
        """Switched off *days_ago*, its purge due in *purge_in* days."""
        now = fields.Datetime.now()
        off = now - timedelta(days=days_ago)
        purge = off + timedelta(days=14) if purge_in is None else now + timedelta(days=purge_in)
        self.inst.write({"pitr_off_at": off, "pitr_purge_at": purge})
        return fields.Datetime.to_string(off)

    def _queue(self):
        """The purges the watch queues: ``[(code, payload)]``."""
        with patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue, \
                patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[1, 2]):
            self.env["cloud.instance"]._cron_pitr_watch()
        return [
            (c.args[3], c.kwargs.get("payload")) for c in enqueue.call_args_list
            if c.args[3] == "pitr_purge"
        ]

    def _purge(self, before):
        return self._executor(ex.PitrPurgeExecutor, "pitr_purge", payload={"before": before})


class TestSwitchingOffFixesThePurgeDate(_SwitchedOffCase):

    def test_switching_off_fixes_it_from_the_window_it_has_then(self):
        self.inst.pitr_state = "ok"
        executor = self._executor(ex.PitrDisableExecutor, "pitr_disable")
        before = fields.Datetime.now()
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.inst.pitr_state, "off")
        self.assertGreaterEqual(self.inst.pitr_off_at, before.replace(microsecond=0))
        self.assertEqual(
            self.inst.pitr_purge_at - self.inst.pitr_off_at, timedelta(days=14),
        )

    def test_a_window_lowered_afterwards_does_not_move_it(self):
        """A plan that lowers the retention switches off too: fixed then."""
        self._switched_off(1)
        purge_at = self.inst.pitr_purge_at
        self.backend.backup_retention = "7D"
        self.inst._pitr_schedule_purge()
        self.assertEqual(self.inst.pitr_purge_at, purge_at)

    def test_one_that_never_archived_gets_no_date(self):
        self.inst.write({"pitr_state": "ok", "pitr_cluster_id": False})
        asyncio.run(self._executor(ex.PitrDisableExecutor, "pitr_disable").on_success({}))
        self.assertFalse(self.inst.pitr_purge_at)


class TestThePurgeWaitsForItsDate(_SwitchedOffCase):

    def test_before_the_date_nothing_is_purged(self):
        self._switched_off(3, purge_in=11)
        self.assertEqual(self._queue(), [])

    def test_once_it_came_the_purge_is_queued_with_its_cutoff(self):
        before = self._switched_off(15)
        self.assertEqual(self._queue(), [("pitr_purge", {"before": before})])

    def test_not_within_a_day_of_a_purge_that_failed(self):
        self._switched_off(15)
        self._purge("x").job.state = "failed"
        self.assertEqual(self._queue(), [])

    def test_not_for_a_stack_configured_outside_the_panel(self):
        self._switched_off(15)
        self.inst.pitr_stack_external = True
        self.assertEqual(self._queue(), [])


class TestThePurge(_SwitchedOffCase):

    def test_it_empties_what_predates_the_switch_off_with_its_own_environment(self):
        before = self._switched_off(15)
        executor = self._purge(before)
        content = executor._env_content()
        self.assertIn(f"DST={PREFIX}\n", content)
        self.assertIn(
            f"PURGE_BEFORE={fields.Datetime.to_datetime(before).isoformat()}\n", content,
        )
        self.assertIn("AWS_SECRET_ACCESS_KEY=pitr-off-secret\n", content)
        self.assertIn("AWS_ENDPOINT_URL=https://r2.example.com\n", content)
        upload = AsyncMock()
        with patch.object(ex.PitrPurgeExecutor, "_upload_private_files", upload):
            asyncio.run(executor.before_execute(object()))
        self.assertEqual(upload.call_args.args[1], {executor._env_path(): content})
        purge, forget = executor.get_commands()
        self.assertEqual(purge[0], ex.PURGE_STEP_LABEL)
        self.assertIn("backup_purge_archived.sh", purge[1])
        self.assertIn(executor._env_path(), purge[1])
        self.assertNotIn("pitr-off-secret", purge[1])
        self.assertTrue(purge[2]["stop_on_failure"])
        self.assertEqual(forget[0], ex.PURGE_FORGET_LABEL)
        self.assertIn(f"cd {self.inst.get_remote_dir()} ", forget[1])
        self.assertIn(f"rm -f {ENV_FILE}", forget[1])

    def test_done_it_forgets_the_archive_and_the_host_gets_no_credentials(self):
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        self.assertIn(ENV_FILE, self.inst._extra_env_files())
        asyncio.run(self._purge(before).on_success({
            ex.PURGE_STEP_LABEL: {"exit_status": 0, "stdout": ""},
            ex.PURGE_FORGET_LABEL: {"exit_status": 0, "stdout": ""},
        }))
        self.assertFalse(self.inst.pitr_cluster_id)
        self.assertFalse(self.inst.pitr_previous_clusters)
        self.assertFalse(self.inst.pitr_off_at)
        self.assertFalse(self.inst.pitr_purge_at)
        self.assertFalse(self.inst.pitr_rendered_prefix)
        self.assertNotIn(ENV_FILE, self.inst._extra_env_files())
        self.assertFalse(self.inst._pitr_had_archive())

    def test_it_empties_the_prefix_rendered_not_the_one_computed_now(self):
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        self.project.remote_folder = "elsewhere"
        self.inst.invalidate_recordset()
        self.assertIn(f"DST={PREFIX}\n", self._purge(before)._env_content())

    def test_a_retry_half_way_finds_nothing_before_the_cutoff_and_finishes(self):
        """The deletion ran, the credentials' removal failed: the next run
        finds the prefix empty before the cutoff (10) and goes on."""
        before = self._switched_off(15)
        first = {
            ex.PURGE_STEP_LABEL: {"exit_status": 0, "stdout": ""},
            ex.PURGE_FORGET_LABEL: {"exit_status": 1, "stdout": ""},
        }
        executor = self._purge(before)
        errors = executor.parse_results(first)
        self.assertEqual(len(errors), 1)
        asyncio.run(executor.on_failure(first, errors))
        alert = self._alerts(ALERT_PURGE_FAILED)
        self.assertIn("was emptied, but", alert.message)
        self.assertEqual(self.inst.pitr_cluster_id, "7698")
        again = {
            ex.PURGE_STEP_LABEL: {"exit_status": 10, "stdout": "IC_PURGE_EMPTY"},
            ex.PURGE_FORGET_LABEL: {"exit_status": 0, "stdout": ""},
        }
        retry = self._purge(before)
        self.assertEqual(retry.parse_results(again), [])
        asyncio.run(retry.on_success(again))
        self.assertFalse(self.inst.pitr_cluster_id)
        self.assertFalse(self._alerts(ALERT_PURGE_FAILED))

    def test_one_the_storage_refused_keeps_the_record_and_says_why(self):
        before = self._switched_off(15)
        results = {ex.PURGE_STEP_LABEL: {"exit_status": 21, "stdout": ""}}
        executor = self._purge(before)
        asyncio.run(executor.on_failure(results, executor.parse_results(results)))
        self.assertIn("rejected", self._alerts(ALERT_PURGE_FAILED).message)
        self.assertEqual(self.inst.pitr_cluster_id, "7698")
        self.assertTrue(self.inst.pitr_purge_at)


class TestSwitchingOnBeforeTheDate(_SwitchedOffCase):

    def test_switching_on_cancels_the_purge_waiting_in_the_queue(self):
        self._switched_off(15)
        waiting = self._purge("x").job
        waiting.state = "queued"
        with patch.object(self.Job, "_cancel_queued", autospec=True) as cancel, \
                patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[7, 8]):
            self.inst.action_pitr_enable()
        self.assertEqual(cancel.call_args.args[1], waiting)
        self.assertFalse(self.inst.pitr_off_at)
        self.assertFalse(self.inst.pitr_purge_at)

    def test_a_plan_that_wants_it_again_keeps_the_archive(self):
        """A layer that switches it with a plan sets the field: the watch's way."""
        self._switched_off(15)
        self.inst.pitr_enabled = True
        with patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[1, 2]) as chain, \
                patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue:
            self.env["cloud.instance"]._cron_pitr_watch()
        self.assertFalse(self.inst.pitr_purge_at)
        self.assertEqual(
            [[s["job_type_code"] for s in c.args[1]] for c in chain.call_args_list],
            [["rebuild_instance", "pitr_enable"]],
        )
        self.assertNotIn("pitr_purge", [c.args[3] for c in enqueue.call_args_list])

    def test_a_purge_that_starts_after_it_empties_nothing(self):
        before = self._switched_off(15)
        self.inst._pitr_cancel_purge()
        executor = self._purge(before)
        self.assertEqual(executor.get_commands(), [(ex.PURGE_IDLE_LABEL, "true")])
        upload = AsyncMock()
        with patch.object(ex.PitrPurgeExecutor, "_upload_private_files", upload):
            asyncio.run(executor.before_execute(object()))
        upload.assert_not_called()
        asyncio.run(executor.on_success({ex.PURGE_IDLE_LABEL: {"exit_status": 0}}))
        self.assertEqual(self.inst.pitr_cluster_id, "7698")

    def test_one_switched_on_while_its_purge_ran_keeps_the_record(self):
        """Its cutoff kept the purge to what predates the switch off."""
        before = self._switched_off(15)
        executor = self._purge(before)
        executor.get_commands()
        self.inst.write({"pitr_enabled": True, "pitr_state": "enabling"})
        self.inst._pitr_cancel_purge()
        asyncio.run(executor.on_success({
            ex.PURGE_STEP_LABEL: {"exit_status": 0, "stdout": ""},
            ex.PURGE_FORGET_LABEL: {"exit_status": 0, "stdout": ""},
        }))
        self.assertEqual(self.inst.pitr_cluster_id, "7698")


class TestAnArchiveSwitchedOffBeforeTheUpdate(_SwitchedOffCase):

    def _disabled(self, days_ago):
        """A ``pitr_disable`` that ended well *days_ago*."""
        job = self._purge("x").job
        job.job_type_id = self.env["cloud.job.type"].search(
            [("code", "=", "pitr_disable")], limit=1,
        )
        job.state = "done"
        self.env.flush_all()
        done = fields.Datetime.now() - timedelta(days=days_ago)
        self.env.cr.execute(
            "UPDATE cloud_job SET date_done = %s WHERE id = %s", (done, job.id),
        )
        job.invalidate_recordset(["date_done"])
        return job

    def test_it_is_dated_from_its_switch_off_when_its_jobs_say(self):
        job = self._disabled(20)
        _run_migration(self.env)
        self.assertEqual(self.inst.pitr_off_at, job.date_done)
        self.assertEqual(
            self.inst.pitr_purge_at, job.date_done + timedelta(days=14),
        )
        self.assertEqual(self.inst.pitr_rendered_prefix, PREFIX)
        self.assertEqual(self.inst.pitr_rendered_backend_id, self.backend)

    def test_else_from_the_update(self):
        before = fields.Datetime.now().replace(microsecond=0)
        _run_migration(self.env)
        self.assertGreaterEqual(self.inst.pitr_off_at, before)
        self.assertEqual(
            self.inst.pitr_purge_at - self.inst.pitr_off_at, timedelta(days=14),
        )

    def test_a_switch_on_after_its_last_switch_off_dates_it_from_the_update(self):
        self._disabled(20)
        enable = self._purge("x").job
        enable.job_type_id = self.env["cloud.job.type"].search(
            [("code", "=", "pitr_enable")], limit=1,
        )
        before = fields.Datetime.now().replace(microsecond=0)
        _run_migration(self.env)
        self.assertGreaterEqual(self.inst.pitr_off_at, before)

    def test_one_archiving_gets_no_date(self):
        self.inst.write({"pitr_enabled": True, "pitr_state": "ok"})
        _run_migration(self.env)
        self.assertFalse(self.inst.pitr_purge_at)
        self.assertEqual(self.inst.pitr_rendered_prefix, PREFIX)


class TestARebuildThatMovesThePrefix(_PitrCase):

    def setUp(self):
        super().setUp()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok", "pitr_cluster_id": "7698",
        })

    def _rebuilt(self):
        with patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[1, 2]), \
                patch.object(self.Job, "enqueue", autospec=True, return_value=3):
            self.inst._after_stack_rendered()

    def test_a_rebuild_keeps_what_it_rendered_and_says_nothing_the_first_time(self):
        self._rebuilt()
        self.assertEqual(self.inst.pitr_rendered_prefix, PREFIX)
        self.assertEqual(self.inst.pitr_rendered_backend_id, self.backend)
        self._rebuilt()
        self.assertFalse(self._alerts(ALERT_PREFIX_CHANGED))

    def test_another_folder_alerts_naming_the_prefix_left(self):
        self._rebuilt()
        self.project.remote_folder = "moved"
        self.inst.invalidate_recordset()
        self._rebuilt()
        alert = self._alerts(ALERT_PREFIX_CHANGED)
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.dedup_key, PREFIX)
        self.assertIn(PREFIX, alert.message)
        self.assertIn("s3://bucket/backups/moved/pitr-inst-pitr", alert.message)
        self.assertIn("RB-23 §4", alert.message)
        self.assertEqual(
            self.inst.pitr_rendered_prefix, "s3://bucket/backups/moved/pitr-inst-pitr",
        )

    def test_the_same_prefix_on_another_destination_alerts_too(self):
        self._rebuilt()
        other = self.env["cloud.backup.backend"].create({
            "name": "pitr-bb-2", "backend_type": "s3", "s3_bucket": "bucket",
            "s3_path": "backups", "s3_endpoint_url": "https://s3.example.net",
            "s3_access_key_id": "AK2", "s3_secret_access_key": "SK2",
            "backup_retention": "14D",
        })
        self.project.backup_backend_id = other
        self.inst.invalidate_recordset()
        self._rebuilt()
        self.assertEqual(len(self._alerts(ALERT_PREFIX_CHANGED)), 1)
        self.assertEqual(self.inst.pitr_rendered_backend_id, other)
