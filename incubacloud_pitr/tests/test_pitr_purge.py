"""The archive of an instance switched off goes once its window passed.

Switching off fixes the purge date (``pitr_off_at`` plus the window it
has then); the watch queues ``pitr_purge`` once it came, which empties
only what was archived before the switch off, with an environment of its
own, then the host's ``pitr.env``, and forgets the archive. Switching on
before the date keeps it; after, it waits for the purge and starts from
nothing. Every rebuild that writes ``pitr.env`` while it archives keeps
the prefix and destination it rendered, and says when they changed.
"""
import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.modules.neutralize import neutralize_database

from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.delete_instance_executor import (
    DeleteInstanceExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

from odoo.addons.incubacloud_pitr.models import pitr_executors as ex
from odoo.addons.incubacloud_pitr.models._constants import (
    ALERT_PREFIX_CHANGED,
    ALERT_PURGE_FAILED,
    ENV_FILE,
)

from odoo.addons.incubacloud.tests.test_backup_dst_shell import _compose_env

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

    def _watch(self):
        """One pass of the watch: ``(chains, enqueued)`` as job type codes."""
        with patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[1, 2]) as chain, \
                patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue:
            self.env["cloud.instance"]._cron_pitr_watch()
        return (
            [[s["job_type_code"] for s in c.args[1]] for c in chain.call_args_list],
            [(c.args[3], c.kwargs.get("payload")) for c in enqueue.call_args_list],
        )

    def _run(self, executor, purge_exit):
        """Run *executor*'s steps through the job's step loop, as a job does.

        :param int purge_exit: what the deletion exits with; every other
            step ends well
        :return: ``(results, skipped, errors)``, *errors* as the job's
            outcome reads them (``_outcome_errors``)
        """
        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = lambda command, *_a, **_k: CommandResult(
            "", purge_exit if "backup_purge_archived.sh" in command else 0,
        )
        results, skipped = asyncio.run(
            executor._run_steps(transport, executor.get_commands()),
        )
        return results, skipped, executor._outcome_errors(results, skipped)


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

    def test_switching_off_again_dates_it_anew(self):
        """A switch on that left the dates (a move's): what was archived
        since is within the new cutoff."""
        self.inst.write({
            "pitr_state": "ok",
            "pitr_off_at": fields.Datetime.now() - timedelta(days=30),
            "pitr_purge_at": fields.Datetime.now() - timedelta(days=16),
        })
        before = fields.Datetime.now().replace(microsecond=0)
        asyncio.run(self._executor(ex.PitrDisableExecutor, "pitr_disable").on_success({}))
        self.assertGreaterEqual(self.inst.pitr_off_at, before)
        self.assertEqual(
            self.inst.pitr_purge_at - self.inst.pitr_off_at, timedelta(days=14),
        )

    def test_switching_off_one_already_off_keeps_the_dates(self):
        """A run on an instance waiting for its purge: the date that came
        stays, or a purge cut short would be lost with a later switch on."""
        off_at = fields.Datetime.now().replace(microsecond=0) - timedelta(days=30)
        purge_at = off_at + timedelta(days=14)
        self.inst.write({
            "pitr_state": "off", "pitr_off_at": off_at, "pitr_purge_at": purge_at,
        })
        asyncio.run(self._executor(ex.PitrDisableExecutor, "pitr_disable").on_success({}))
        self.assertEqual(self.inst.pitr_off_at, off_at)
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
        self.assertNotIn("AWS_DEFAULT_REGION", content)
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
        self.assertEqual(purge[2]["continue_on"], (10,))
        self.assertEqual(forget[0], ex.PURGE_FORGET_LABEL)
        self.assertIn(f"cd {self.inst.get_remote_dir()} ", forget[1])
        self.assertIn(f"rm -f {ENV_FILE}", forget[1])

    def test_the_destination_s_region_signs_the_purge(self):
        """boto3 reads it from ``AWS_DEFAULT_REGION``, as the backup container."""
        before = self._switched_off(15)
        self.backend.s3_region = "eu-west-par"
        content = self._purge(before)._env_content()
        self.assertTrue(content.endswith("AWS_DEFAULT_REGION=eu-west-par\n"))

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

    def test_an_archive_already_empty_goes_on_and_the_job_ends_well(self):
        """10 through the job's own loop: the credentials' removal still
        runs, nothing is skipped, and the outcome is a success."""
        before = self._switched_off(15)
        results, skipped, errors = self._run(self._purge(before), 10)
        self.assertEqual(skipped, [])
        self.assertIn(ex.PURGE_FORGET_LABEL, results)
        self.assertEqual(errors, [])

    def test_a_deletion_that_fails_stops_before_the_credentials(self):
        before = self._switched_off(15)
        results, skipped, errors = self._run(self._purge(before), 21)
        self.assertEqual(skipped, [ex.PURGE_FORGET_LABEL])
        self.assertNotIn(ex.PURGE_FORGET_LABEL, results)
        self.assertTrue(errors)

    def test_one_queued_before_the_date_empties_nothing(self):
        """Queued by hand with the right cutoff: the date is the job's too."""
        before = self._switched_off(3, purge_in=11)
        self.assertEqual(
            self._purge(before).get_commands(), [(ex.PURGE_IDLE_LABEL, "true")],
        )

    def test_one_queued_on_a_server_the_instance_left_empties_nothing(self):
        before = self._switched_off(15)
        executor = self._purge(before)
        self.inst.host_id = self.env["cloud.host"].create({
            "name": "pitr-host-2", "ip_address": "192.0.2.91", "user": "ubuntu",
            "wildcard_domain": "pitr2.example.com",
        })
        self.assertEqual(executor.get_commands(), [(ex.PURGE_IDLE_LABEL, "true")])

    def test_on_a_managed_destination_its_failure_goes_to_the_provider(self):
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        self.backend.sudo().managed_reservation_id = "managed-1"
        results = {ex.PURGE_STEP_LABEL: {"exit_status": 21, "stdout": ""}}
        executor = self._purge(before)
        asyncio.run(executor.on_failure(results, executor.parse_results(results)))
        self.assertEqual(self._alerts(ALERT_PURGE_FAILED).audience, "provider")

    def test_one_the_storage_refused_keeps_the_record_and_says_why(self):
        before = self._switched_off(15)
        results = {ex.PURGE_STEP_LABEL: {"exit_status": 21, "stdout": ""}}
        executor = self._purge(before)
        asyncio.run(executor.on_failure(results, executor.parse_results(results)))
        self.assertIn("rejected", self._alerts(ALERT_PURGE_FAILED).message)
        self.assertEqual(self.inst.pitr_cluster_id, "7698")
        self.assertTrue(self.inst.pitr_purge_at)


class TestThePurgeEnvironmentKeepsItsLines(_SwitchedOffCase):

    def test_a_prefix_that_breaks_its_line_fails_the_job_before_the_host(self):
        """Its environment file would read the rest as variables of its own."""
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET pitr_rendered_prefix = %s WHERE id = %s",
            (f"{PREFIX}\nAWS_ENDPOINT_URL=https://elsewhere.example", self.inst.id),
        )
        self.env.invalidate_all()
        executor = self._purge(before)
        with patch.object(
            ex.PitrPurgeExecutor, "_upload_private_files", autospec=True,
        ) as upload, self.assertRaises(ValueError):
            asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
        upload.assert_not_awaited()

    def test_an_endpoint_or_a_key_saved_before_that_names_dst_again_fails_it(self):
        """``--env-file`` takes the last DST: the destination's root, emptied."""
        before = self._switched_off(15)
        executor = self._purge(before)
        for column, value in (
            ("s3_endpoint_url", "https://r2.example.com\nDST=s3://bucket/backups"),
            ("s3_access_key_id", "AK\nPURGE_BEFORE="),
            ("s3_secret_access_key", "SK\rDST=s3://bucket"),
        ):
            with self.subTest(column=column):
                self.env.flush_all()
                self.env.cr.execute(
                    f"UPDATE cloud_backup_backend SET {column} = %s WHERE id = %s",  # noqa: S608
                    (value, self.backend.id),
                )
                self.env.invalidate_all()
                with patch.object(
                    ex.PitrPurgeExecutor, "_upload_private_files", autospec=True,
                ) as upload, self.assertRaises(ValueError):
                    asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
                upload.assert_not_awaited()
                self.env.cr.execute(
                    f"UPDATE cloud_backup_backend SET {column} = %s WHERE id = %s",  # noqa: S608
                    ("clean", self.backend.id),
                )
                self.env.invalidate_all()

    def test_the_agent_s_environment_refuses_a_key_that_breaks_its_line(self):
        """``pitr.env`` is read by the archive's agent and the delete's purge."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_backup_backend SET s3_access_key_id = %s WHERE id = %s",
            ("AK\nIC_PITR_PREFIX=s3://bucket", self.backend.id),
        )
        self.env.invalidate_all()
        with self.assertRaises(ValueError):
            self.inst._pitr_env()

    def test_the_agent_s_environment_refuses_what_compose_reads_as_more(self):
        """The agent's ``env_file``: a quote would add an ``IC_PITR_PREFIX``."""
        for column, value in (
            ("s3_secret_access_key", '"SK" IC_PITR_PREFIX=s3://bucket'),
            ("s3_access_key_id", '"AK"IC_PITR_PREFIX=s3://bucket'),
            ("s3_access_key_id", "AK$PGHOST"),
        ):
            with self.subTest(column=column, value=value):
                self.env.flush_all()
                self.env.cr.execute(
                    f"UPDATE cloud_backup_backend SET {column} = %s WHERE id = %s",  # noqa: S608
                    (value, self.backend.id),
                )
                self.env.invalidate_all()
                with self.assertRaises(ValueError):
                    self.inst._pitr_env()

    def test_the_agent_s_environment_reaches_it_as_written(self):
        content = self.inst._pitr_env()
        written = dict(line.split("=", 1) for line in content.splitlines())
        self.assertEqual(_compose_env(content), written)
        self.assertEqual(written["IC_PITR_PREFIX"], PREFIX)


class TestTheDeleteReachesOnlyItsOwnArchive(_PitrCase):
    """Deleting an instance empties its recovery prefix whole, or only what
    it stored since it was created when an archived instance of the same
    name keeps its archive there."""

    def setUp(self):
        super().setUp()
        self.inst.pitr_enabled = True
        # A staging: a project has one production.
        self.other = self.env["cloud.instance"].create({
            "name": "pitr-other", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed",
        })

    def test_the_recovery_prefix_is_checked_with_the_chain_s(self):
        executor = self._executor(DeleteInstanceExecutor, "delete_instance")
        self.assertIn(PREFIX, executor._purge_prefixes(self.inst))
        executor._refuse_shared_prefix(self.inst)

    def test_another_s_copy_under_it_refuses_the_delete_before_any_step(self):
        """Saved before the check: a write of it now is refused."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET custom_backup_dst = %s WHERE id = %s",
            (f"{PREFIX}/held", self.other.id),
        )
        self.env.invalidate_all()
        executor = self._executor(DeleteInstanceExecutor, "delete_instance")
        with self.assertRaises(JobRefused) as caught:
            asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
        self.assertIn("pitr-other", str(caught.exception))
        self.assertIn(PREFIX, str(caught.exception))
        self.assertEqual(self.inst.state, "deployed")

    def test_archiving_it_keeps_the_prefix_and_is_not_refused(self):
        executor = self._executor(
            DeleteInstanceExecutor, "delete_instance", payload={"keep_in_panel": True},
        )
        self.assertEqual(executor._purge_prefixes(self.inst), [])

    def _archived_namesake(self, prefix):
        """An archived instance of this name whose recovery archive is *prefix*.

        :param str prefix: its frozen recovery prefix
        :return: the archived ``cloud.instance``
        """
        project = self.env["cloud.project"].create({
            "name": "pitr-proj-2", "remote_folder": "pp2",
            "backup_backend_id": self.backend.id,
        })
        namesake = self.env["cloud.instance"].create({
            "name": "pitr-inst", "project_id": project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed",
        })
        # Archived two days before this instance was created: the dates
        # prove the succession (``_backup_succession``).
        now = fields.Datetime.now()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET active = false, pitr_archived_prefix = %s,"
            " create_date = %s, archived_at = %s WHERE id = %s",
            (prefix, now - timedelta(days=9), now - timedelta(days=2), namesake.id),
        )
        self.env.invalidate_all()
        return namesake

    def test_an_archived_one_without_its_date_refuses_the_delete(self):
        """Its archive is not told apart from this one's by the dates."""
        namesake = self._archived_namesake(PREFIX)
        namesake.sudo().write({"archived_at": False})
        executor = self._executor(DeleteInstanceExecutor, "delete_instance")
        with self.assertRaises(JobRefused) as caught:
            executor._refuse_shared_prefix(self.inst)
        self.assertIn(f"id {namesake.id}", str(caught.exception))

    def test_a_successor_s_delete_keeps_the_archived_one_s_archive(self):
        self._archived_namesake(PREFIX)
        executor = self._executor(DeleteInstanceExecutor, "delete_instance")
        executor._refuse_shared_prefix(self.inst)
        since = self.inst.create_date.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for step in self.inst._teardown_pre_steps("~/pp/pitr-inst", False):
            with self.subTest(step=step[0]):
                self.assertIn(
                    f" delete --use-server-modtime --max-age {since} pitr: ",
                    step[1],
                )
                self.assertNotIn("purge pitr:", step[1])

    def test_without_one_in_its_prefix_the_delete_empties_it_whole(self):
        self._archived_namesake("s3://bucket/backups/pp2/pitr-inst-pitr")
        for step in self.inst._teardown_pre_steps("~/pp/pitr-inst", False):
            with self.subTest(step=step[0]):
                self.assertIn(" purge pitr: ", step[1])
                self.assertNotIn("--max-age", step[1])


class TestThePurgeReachesOnlyItsOwnArchive(_SwitchedOffCase):
    """A prefix that is a destination's, or holds another instance's, is refused."""

    def _rendered(self, prefix):
        """Store *prefix* as the one the instance's server was last given."""
        self.inst._pitr_record_rendered()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET pitr_rendered_prefix = %s WHERE id = %s",
            (prefix, self.inst.id),
        )
        self.env.invalidate_all()

    def test_a_destination_s_path_fails_closed_with_an_alert(self):
        before = self._switched_off(15)
        # A staging: a project has one production.
        self.env["cloud.instance"].create({
            "name": "pitr-other", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed",
        })
        for prefix in ("s3://bucket/backups", "s3://bucket", "s3://bucket/backups/pp"):
            with self.subTest(prefix=prefix):
                self._rendered(prefix)
                executor = self._purge(before)
                with patch.object(
                    ex.PitrPurgeExecutor, "_upload_private_files", autospec=True,
                ) as upload, self.assertRaises(JobRefused):
                    asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
                upload.assert_not_awaited()
        asyncio.run(executor.on_abort(JobRefused("refused")))
        alert = self._alerts(ALERT_PURGE_FAILED)
        self.assertEqual(len(alert), 1)
        self.assertIn("pitr-other", alert.message)

    def _elsewhere(self, name, **vals):
        """A production of *name* archiving, in a project of its own on the destination.

        :param str name: the instance's name
        :param vals: more of its values
        :return: the instance
        """
        project = self.env["cloud.project"].create({
            "name": f"{name}-proj", "remote_folder": "pp2",
            "backup_backend_id": self.backend.id,
        })
        return self.env["cloud.instance"].create({
            "name": name, "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "pitr_enabled": True,
        } | vals)

    def test_another_instance_s_archive_is_not_emptied(self):
        before = self._switched_off(15)
        other = self._elsewhere("pitr-other")
        self._rendered(other._pitr_prefix_base())
        with self.assertRaises(JobRefused) as caught:
            self._purge(before)._env_content()
        self.assertIn("pitr-other", str(caught.exception))

    def test_a_live_one_of_the_same_name_in_its_prefix_is_refused(self):
        """Two live ones in a prefix prove no succession."""
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        other = self._elsewhere("pitr-inst", pitr_rendered_prefix=PREFIX)
        with self.assertRaises(JobRefused) as caught:
            self._purge(before)._env_content()
        self.assertIn(f"id {other.id}", str(caught.exception))

    def test_the_cutoff_ends_where_one_that_took_the_name_began(self):
        """The instance archived since, its name taken: the bound is the
        switch off or the successor's creation, the earlier."""
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        successor = self._elsewhere("pitr-inst", pitr_rendered_prefix=PREFIX)
        now = fields.Datetime.now()
        for archived, created, bound in (
            (16, 15.5, 15.5),   # a successor before the switch off: its creation
            (14, 13, None),     # one after it: the switch off
        ):
            with self.subTest(created=created):
                self.env.flush_all()
                self.env.cr.execute(
                    "UPDATE cloud_instance SET active = false, archived_at = %s,"
                    " create_date = %s WHERE id = %s",
                    (now - timedelta(days=archived), now - timedelta(days=30),
                     self.inst.id),
                )
                self.env.cr.execute(
                    "UPDATE cloud_instance SET create_date = %s WHERE id = %s",
                    (now - timedelta(days=created), successor.id),
                )
                self.env.invalidate_all()
                expected = (
                    now - timedelta(days=bound) if bound
                    else fields.Datetime.to_datetime(before)
                )
                self.assertIn(
                    f"PURGE_BEFORE={expected.isoformat()}\n",
                    self._purge(before)._env_content(),
                )

    def test_a_successor_s_purge_starts_at_its_creation(self):
        """This one took the name of one archived that keeps its archive in
        the prefix: what that one archived before stays (``PURGE_SINCE``)."""
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        predecessor = self._elsewhere("pitr-inst", pitr_rendered_prefix=PREFIX)
        now = fields.Datetime.now()
        born = now - timedelta(days=19)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET active = false, archived_at = %s,"
            " create_date = %s WHERE id = %s",
            (now - timedelta(days=20), now - timedelta(days=30), predecessor.id),
        )
        self.env.cr.execute(
            "UPDATE cloud_instance SET create_date = %s WHERE id = %s",
            (born, self.inst.id),
        )
        self.env.invalidate_all()
        content = self._purge(before)._env_content()
        self.assertIn(
            f"PURGE_SINCE={born.strftime('%Y-%m-%dT%H:%M:%S+00:00')}\n", content,
        )
        self.assertIn(
            f"PURGE_BEFORE={fields.Datetime.to_datetime(before).isoformat()}\n",
            content,
        )

    def test_without_one_of_its_name_in_the_prefix_it_has_no_start(self):
        before = self._switched_off(15)
        self.inst._pitr_record_rendered()
        self.assertNotIn("PURGE_SINCE", self._purge(before)._env_content())

class TestSwitchingOnBeforeTheDate(_SwitchedOffCase):

    def test_switching_on_cancels_the_purge_waiting_in_the_queue(self):
        self._switched_off(3, purge_in=11)
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
        self._switched_off(3, purge_in=11)
        self.inst.pitr_enabled = True
        chains, enqueued = self._watch()
        self.assertFalse(self.inst.pitr_purge_at)
        self.assertEqual(chains, [["rebuild_instance", "pitr_enable"]])
        self.assertNotIn("pitr_purge", [code for code, _payload in enqueued])

    def test_a_purge_that_starts_after_it_empties_nothing(self):
        before = self._switched_off(3, purge_in=11)
        self.inst._pitr_cancel_purge()
        executor = self._purge(before)
        self.assertEqual(executor.get_commands(), [(ex.PURGE_IDLE_LABEL, "true")])
        upload = AsyncMock()
        with patch.object(ex.PitrPurgeExecutor, "_upload_private_files", upload):
            asyncio.run(executor.before_execute(object()))
        upload.assert_not_called()
        asyncio.run(executor.on_success({ex.PURGE_IDLE_LABEL: {"exit_status": 0}}))
        self.assertEqual(self.inst.pitr_cluster_id, "7698")



class TestSwitchingOnOnceTheDateCame(_SwitchedOffCase):
    """The purge may have started: a switch on waits for it to end.

    The deletion goes oldest first, so a purge cut short leaves deltas
    without their base; a switch on then would take its first copy as a
    delta over them, and show a window that cannot be restored.
    """

    def test_the_card_refuses_and_keeps_the_dates(self):
        self._switched_off(15)
        with patch.object(self.Job, "enqueue_chain", autospec=True) as chain:
            with self.assertRaises(UserError):
                self.inst.action_pitr_enable()
        chain.assert_not_called()
        self.assertTrue(self.inst.pitr_purge_at)
        self.assertFalse(self.inst.pitr_enabled)

    def test_a_plan_that_wants_it_waits_for_the_purge_then_starts_from_nothing(self):
        before = self._switched_off(15)
        self.inst.pitr_enabled = True
        chains, enqueued = self._watch()
        self.assertEqual(chains, [])
        self.assertEqual(enqueued, [("pitr_purge", {"before": before})])
        self.assertTrue(self.inst.pitr_purge_at)
        executor = self._purge(before)
        # Its stack names pitr.env again: the purge leaves it.
        self.assertEqual(
            [step[0] for step in executor.get_commands()], [ex.PURGE_STEP_LABEL],
        )
        asyncio.run(executor.on_success({
            ex.PURGE_STEP_LABEL: {"exit_status": 0, "stdout": ""},
        }))
        executor.job.state = "done"
        self.assertFalse(self.inst.pitr_cluster_id)
        self.assertFalse(self.inst.pitr_purge_at)
        chains, _enqueued = self._watch()
        self.assertEqual(chains, [["rebuild_instance", "pitr_enable"]])

    def test_after_a_purge_that_failed_it_still_waits(self):
        """Cut short: switched on now, the first copy would be a delta
        over what the deletion left."""
        before = self._switched_off(15)
        results = {ex.PURGE_STEP_LABEL: {"exit_status": 22, "stdout": ""}}
        failed = self._purge(before)
        asyncio.run(failed.on_failure(results, failed.parse_results(results)))
        failed.job.state = "failed"
        self.inst.pitr_enabled = True
        self.inst._pitr_cancel_purge()
        chains, enqueued = self._watch()
        self.assertEqual(chains, [])
        self.assertNotIn("pitr_purge", [code for code, _payload in enqueued])
        self.assertTrue(self.inst.pitr_purge_at)
        self.assertEqual(self.inst.pitr_cluster_id, "7698")
        self.assertTrue(self._alerts(ALERT_PURGE_FAILED))
        with self.assertRaises(UserError):
            self.inst.action_pitr_enable()

    def test_a_switch_on_while_its_purge_runs_does_not_hold_the_forgetting(self):
        before = self._switched_off(15)
        executor = self._purge(before)
        executor.get_commands()
        self.inst.pitr_enabled = True
        self.inst._pitr_cancel_purge()
        self.assertTrue(self.inst.pitr_purge_at)
        asyncio.run(executor.on_success({
            ex.PURGE_STEP_LABEL: {"exit_status": 0, "stdout": ""},
            ex.PURGE_FORGET_LABEL: {"exit_status": 0, "stdout": ""},
        }))
        self.assertFalse(self.inst.pitr_cluster_id)


class TestAnArchiveSwitchedOffBeforeTheUpdate(_SwitchedOffCase):

    def test_it_is_dated_from_the_update(self):
        """Switched off when the archive stayed: the whole window from now."""
        before = fields.Datetime.now().replace(microsecond=0)
        _run_migration(self.env)
        self.assertGreaterEqual(self.inst.pitr_off_at, before)
        self.assertEqual(
            self.inst.pitr_purge_at - self.inst.pitr_off_at, timedelta(days=14),
        )
        self.assertEqual(self.inst.pitr_rendered_prefix, PREFIX)
        self.assertEqual(self.inst.pitr_rendered_backend_id, self.backend)

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


class TestARebuildWhileSwitchedOff(_SwitchedOffCase):
    """Nothing archives at the new prefix: the purge keeps the old one."""

    def _rebuilt(self):
        with patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[1, 2]), \
                patch.object(self.Job, "enqueue", autospec=True, return_value=3):
            self.inst._after_stack_rendered()

    def test_another_folder_keeps_the_prefix_and_says_nothing(self):
        before = self._switched_off(3, purge_in=11)
        self.inst._pitr_record_rendered()
        self.project.remote_folder = "moved"
        self.inst.invalidate_recordset()
        self._rebuilt()
        self.assertFalse(self._alerts(ALERT_PREFIX_CHANGED))
        self.assertEqual(self.inst.pitr_rendered_prefix, PREFIX)
        self.assertEqual(self.inst.pitr_rendered_backend_id, self.backend)
        self.assertIn(f"DST={PREFIX}\n", self._purge(before)._env_content())

    def test_switched_on_again_the_rebuild_says_it(self):
        self._switched_off(3, purge_in=11)
        self.inst._pitr_record_rendered()
        self.project.remote_folder = "moved"
        self.inst.write({"pitr_enabled": True, "pitr_state": "enabling"})
        self.inst.invalidate_recordset()
        self._rebuilt()
        self.assertEqual(len(self._alerts(ALERT_PREFIX_CHANGED)), 1)
        self.assertEqual(
            self.inst.pitr_rendered_prefix, "s3://bucket/backups/moved/pitr-inst-pitr",
        )


class TestArchivingFreezesTheRenderedPrefix(_SwitchedOffCase):

    def test_the_prefix_its_server_archived_to_not_the_one_computed_now(self):
        self.inst._pitr_record_rendered()
        self.project.remote_folder = "moved"
        self.inst.invalidate_recordset()
        self.inst._freeze_backup_dst()
        self.assertEqual(self.inst.pitr_archived_prefix, PREFIX)


class TestANeutralizedCopy(_SwitchedOffCase):

    def test_it_keeps_no_date_cluster_nor_prefix_of_production(self):
        self._switched_off(15)
        self.inst._pitr_record_rendered()
        self.env.flush_all()
        neutralize_database(self.env.cr)
        self.env.invalidate_all()
        self.assertFalse(self.inst.pitr_cluster_id)
        self.assertFalse(self.inst.pitr_previous_clusters)
        self.assertFalse(self.inst.pitr_off_at)
        self.assertFalse(self.inst.pitr_purge_at)
        self.assertFalse(self.inst.pitr_rendered_prefix)
        self.assertFalse(self.inst.pitr_rendered_backend_id)


class TestASuccessorArchivesUnderAPrefixOfItsOwn(_PitrCase):
    """The recovery prefix follows the backup path's suffix (``backup_path_suffix``).

    An instance that takes the name of an archived one whose project's
    folder it took computes ``<folder>/<name>-<id>-pitr``: its archive is
    not the archived one's, and neither purge reaches the other.
    """

    def setUp(self):
        super().setUp()
        self.inst.write({"pitr_enabled": True, "pitr_cluster_id": "7698"})
        self.inst._finalize_removal(keep_in_panel=True)
        self.assertEqual(self.inst.pitr_archived_prefix, PREFIX)
        self.project.remote_folder = "pp-archived"
        self.again = self.env["cloud.project"].create({
            "name": "pitr-proj-again", "remote_folder": "pp",
            "backup_backend_id": self.backend.id,
        })

    def _successor(self):
        return self.env["cloud.instance"].create({
            "name": "pitr-inst", "project_id": self.again.id,
            "environment": "production", "host_id": self.host.id,
            "state": "draft",
        })

    def test_its_recovery_prefix_follows_the_suffix(self):
        successor = self._successor()
        self.assertEqual(successor.backup_path_suffix, f"-{successor.id}")
        self.assertEqual(
            successor._pitr_prefix_base(),
            f"s3://bucket/backups/pp/pitr-inst-{successor.id}-pitr",
        )
        self.assertIsNone(self.inst._backup_prefix_conflict(PREFIX))

    def test_an_archive_left_alone_under_the_name_claims_it_too(self):
        """Its chain path elsewhere: the recovery prefix alone ends as the
        successor's would (``_backup_path_tails``)."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET custom_backup_dst = %s WHERE id = %s",
            ("boto3+s3://bucket/imported/pitr-inst", self.inst.id),
        )
        self.env.invalidate_all()
        successor = self._successor()
        self.assertEqual(successor.backup_path_suffix, f"-{successor.id}")

    def test_its_chain_path_still_says_its_prefix_once_archived(self):
        successor = self._successor()
        successor._finalize_removal(keep_in_panel=True)
        successor.sudo().pitr_archived_prefix = False
        self.assertEqual(
            successor._pitr_prefix_from_chain(),
            f"s3://bucket/backups/pp/pitr-inst-{successor.id}-pitr",
        )
