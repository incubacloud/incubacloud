"""Deleting an archived instance must take its chain with it.

An archived instance is the only thing that still knows where its
objects are: the path is frozen on the record precisely because the
computed one stops being true once the project moves underneath it. So
an ``unlink`` would not merely leave the chain behind — it would leave
it unreachable, with no instance, no project and no path left to find it
by. That is the exact failure the whole archiving feature exists to
prevent, arriving through the back door.

What these pin is the order (objects first, record second, never the
other way round) and the refusals: nothing here is allowed to remove a
record while its copy still exists.
"""
import asyncio

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.purge_archived_executor import (
    PURGE_ARCHIVED_ALERT_BY_EXIT,
    PURGE_ARCHIVED_LABEL,
    PurgeArchivedBackupsExecutor,
)


class _ArchivedPurgeBase(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "pa-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "pa-host", "ip_address": "10.0.12.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "pa.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "pa-backend",
            "backend_type": "s3",
            "s3_bucket": "pa-bucket",
            "s3_path": "backups",
            "s3_access_key_id": "AKIAPA",
            "s3_secret_access_key": "shh",
            "s3_endpoint_url": "https://pa.example.net",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "pa-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed",
            "backup_backend_id": self.backend.id,
        })

    def _archive(self):
        self.instance._finalize_removal(keep_in_panel=True)
        return self.instance

    def _executor(self, job):
        executor = object.__new__(PurgeArchivedBackupsExecutor)
        executor.job = job
        executor.env = job.env
        executor._log_buffer = []
        executor._scripts_requested = False
        executor._scripts_uploaded = False
        executor._script_overlay_cache = None
        return executor


class TestDeleteArchivedGuards(_ArchivedPurgeBase):

    def test_a_live_instance_is_refused(self):
        with self.assertRaises(UserError):
            self.instance.delete_archived()

    def test_an_archived_instance_enqueues_the_purge(self):
        inst = self._archive()
        job_ids = inst.delete_archived()
        self.assertTrue(job_ids)
        job = self.env["cloud.job"].browse(job_ids[0])
        self.assertEqual(job.job_type_id.code, "purge_archived_backups")
        self.assertEqual(job.instance_id, inst)
        # Still there: the record only goes when the purge succeeded.
        self.assertTrue(inst.exists())

    def test_an_instance_with_no_copy_is_unlinked_outright(self):
        """Nothing was ever stored, so there is nothing to empty first."""
        # All three levels: the effective backend falls back from the
        # instance to the project and then to the global default.
        self.instance.backup_backend_id = False
        self.project.backup_backend_id = False
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.instance.invalidate_recordset()
        inst = self._archive()
        self.assertFalse(inst.custom_backup_dst)
        self.assertEqual(inst.delete_archived(), [])
        self.assertFalse(inst.exists())

    def test_a_copy_with_no_host_left_is_refused(self):
        """Refusing is the safe answer: unlinking would strand the chain
        with nothing left that knows where it is."""
        inst = self._archive()
        inst.sudo().write({"host_id": False})
        with self.assertRaises(UserError):
            inst.delete_archived()
        self.assertTrue(inst.exists())


class TestPurgeCutoff(_ArchivedPurgeBase):
    """The purge is bounded to the chain that existed when it was decided.

    A prefix is derived from the instance name, so a new instance taking
    that name inherits it. Everything here protects the same thing: that
    a purge landing late never deletes the successor's backups.
    """

    def test_the_first_call_stamps_the_cutoff(self):
        inst = self._archive()
        self.assertFalse(inst.purge_cutoff_at)
        inst.delete_archived()
        self.assertTrue(inst.purge_cutoff_at)

    def test_a_retry_keeps_the_original_cutoff(self):
        """The whole mechanism. Re-stamping on retry would set the bound
        to "now", by which time the successor's chain is older than it
        and would be deleted.

        The retry is reached the way production reaches it — the first
        job failing and the purge being asked for again — because the
        active-job guard refuses a second one while the first is alive.
        """
        inst = self._archive()
        job = self.env["cloud.job"].browse(inst.delete_archived()[0])
        first = inst.purge_cutoff_at
        job.sudo().write({"state": "failed"})
        inst.delete_archived()
        self.assertEqual(inst.purge_cutoff_at, first)

    def test_reviving_clears_the_cutoff(self):
        """A deletion that was decided and then reverted must not bound a
        future purge: that purge would predate the chain written since,
        delete nothing, and report success."""
        inst = self._archive()
        inst.delete_archived()
        self.assertTrue(inst.purge_cutoff_at)
        inst.sudo().write({
            "active": True, "archived_at": False, "purge_cutoff_at": False,
        })
        self.assertFalse(inst.purge_cutoff_at)

    def test_an_instance_with_no_copy_is_not_stamped(self):
        """It unlinks on the spot — there is no job to bound."""
        self.instance.backup_backend_id = False
        self.project.backup_backend_id = False
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.instance.invalidate_recordset()
        inst = self._archive()
        self.assertEqual(inst.delete_archived(), [])
        self.assertFalse(inst.exists())

    def test_the_environment_carries_the_cutoff(self):
        inst = self._archive()
        job = self.env["cloud.job"].browse(inst.delete_archived()[0])
        env_text = self._executor(job)._env_content()
        self.assertIn(
            f"PURGE_BEFORE={inst.purge_cutoff_at.isoformat()}", env_text,
        )


class TestPurgeArchivedExecutor(_ArchivedPurgeBase):

    def _job(self):
        return self.env["cloud.job"].browse(
            self._archive().delete_archived()[0],
        )

    def test_the_environment_carries_the_frozen_path(self):
        """Never the computed one: after the project moves, the computed
        path points somewhere that is not this instance's backups."""
        job = self._job()
        inst = job.with_context(active_test=False).instance_id
        env_text = self._executor(job)._env_content()
        self.assertIn(f"DST={inst.custom_backup_dst}", env_text)
        self.assertIn("AWS_ACCESS_KEY_ID=AKIAPA", env_text)
        self.assertIn("AWS_SECRET_ACCESS_KEY=shh", env_text)
        self.assertIn("AWS_ENDPOINT_URL=https://pa.example.net", env_text)

    def test_the_command_passes_no_secret_as_an_argument(self):
        """Arguments are visible in ``ps`` to every account on the host
        for as long as the container runs."""
        job = self._job()
        commands = self._executor(job).get_commands()
        self.assertEqual(len(commands), 1)
        label, command = commands[0][0], commands[0][1]
        self.assertEqual(label, PURGE_ARCHIVED_LABEL)
        self.assertNotIn("shh", command)
        self.assertNotIn("AKIAPA", command)
        self.assertIn("backup_purge_archived.sh", command)

    def test_the_step_aborts_the_run_when_it_fails(self):
        job = self._job()
        opts = self._executor(job).get_commands()[0][2]
        self.assertTrue(opts.get("stop_on_failure"))

    def test_an_already_empty_prefix_is_not_a_failure(self):
        """The invariant already holds, so the record may go."""
        self.assertEqual(
            self._executor(self._job()).parse_results({
                PURGE_ARCHIVED_LABEL: {"exit_status": 10, "stdout": ""},
            }),
            [],
        )

    def test_a_real_failure_is_reported(self):
        self.assertTrue(
            self._executor(self._job()).parse_results({
                PURGE_ARCHIVED_LABEL: {"exit_status": 22, "stdout": ""},
            })
        )

    def test_success_unlinks_the_record(self):
        job = self._job()
        inst = job.with_context(active_test=False).instance_id
        inst_id = inst.id
        executor = self._executor(job)
        asyncio.run(executor.on_success({
            PURGE_ARCHIVED_LABEL: {"exit_status": 0, "stdout": ""},
        }))
        self.assertFalse(
            self.env["cloud.instance"].with_context(
                active_test=False,
            ).browse(inst_id).exists()
        )

    def test_failure_keeps_the_record_and_names_the_cause(self):
        job = self._job()
        inst = job.with_context(active_test=False).instance_id
        executor = self._executor(job)
        asyncio.run(executor.on_failure(
            {PURGE_ARCHIVED_LABEL: {"exit_status": 21, "stdout": ""}},
            ["purge failed"],
        ))
        self.assertTrue(inst.exists())
        code = PURGE_ARCHIVED_ALERT_BY_EXIT[21][0]
        self.assertTrue(self.env["cloud.alert"].sudo().search([
            ("code", "=", code),
            ("instance_id", "=", inst.id),
            ("state", "=", "active"),
        ]))

    def test_an_unclassified_exit_still_alerts(self):
        """Silence would be the worst outcome: the copy is still there
        and nothing said so."""
        job = self._job()
        inst = job.with_context(active_test=False).instance_id
        executor = self._executor(job)
        asyncio.run(executor.on_failure(
            {PURGE_ARCHIVED_LABEL: {"exit_status": 137, "stdout": ""}},
            ["killed"],
        ))
        self.assertTrue(self.env["cloud.alert"].sudo().search([
            ("code", "=", PURGE_ARCHIVED_ALERT_BY_EXIT[22][0]),
            ("instance_id", "=", inst.id),
            ("state", "=", "active"),
        ]))
