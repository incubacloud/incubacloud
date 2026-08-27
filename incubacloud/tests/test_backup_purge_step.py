"""Deleting an instance clears its backups first, or does not happen.

The invariant: never leave objects belonging to an instance that no
longer exists. Nothing prunes them afterwards — the retention job runs
inside the very container the teardown destroys — and on a managed
destination they keep consuming a paid quota the customer cannot reach.

That forces the order. The only thing that can talk to the bucket is the
instance's own ``backup`` container, so the purge runs *before* the
teardown and, if it fails, aborts it. Tearing down first would strand the
objects with nothing left able to delete them, which is why there is no
"delete anyway".

What these pin, in order of how badly each would hurt:

  * the step never reaches a teardown that is not a deletion — the move
    cleanups reuse these very commands against a host the instance has
    left, while it is alive and using those backups elsewhere;
  * it runs before the teardown, and stops it on failure;
  * Free tenants are excluded by the same gate that decides whether the
    container is deployed at all, not by a branch naming them;
  * each exit code produces the alert that names its own fix, and a
    clean run clears them.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.delete_instance_executor import (
    ARCHIVE_ALERT_BY_EXIT,
    ARCHIVE_LABEL,
    PURGE_ALERT_BY_EXIT,
    PURGE_EXIT_ALREADY_EMPTY,
    PURGE_LABEL,
    DeleteInstanceExecutor,
)
from ..models.move_cutover_executor import MoveCleanupSourceExecutor
from ..models.move_rollback_cleanup_executor import (
    MoveRollbackCleanupExecutor,
)

_TEARDOWN_FIRST_STEP = "Stop and remove containers"


class _PurgeBase(TransactionCase):

    def setUp(self):
        # Executors write alerts on their own cursor; without test mode
        # that connection cannot see this transaction's records and the
        # INSERT dies on the instance/host foreign key.
        self.registry_enter_test_mode()
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "pg-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "pg-host", "ip_address": "10.0.8.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "pg.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "pg-backend",
            "backend_type": "s3",
            "s3_bucket": "pg-bucket",
            "s3_path": "backups",
            "s3_access_key_id": "AKIAPG",
            "s3_secret_access_key": "shh",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "pg-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed",
            "backup_backend_id": self.backend.id,
        })

    def _job(self, code="delete_instance", payload=None, instance=None):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        job = self.env["cloud.job"].create({
            "host_id": self.host.id,
            "instance_id": (instance or self.instance).id,
            "job_type_id": job_type.id,
            "name": f"{code} purge test",
        })
        job.payload = payload or {}
        return job

    def _executor(self, job, cls=DeleteInstanceExecutor):
        ex = object.__new__(cls)
        ex.job = job
        ex.env = job.env
        ex._log_buffer = []
        ex._scripts_requested = False
        ex._scripts_uploaded = False
        ex._script_overlay_cache = None
        return ex

    def _labels(self, ex):
        return [step[0] for step in ex.get_commands()]


class TestPurgeStepPlacement(_PurgeBase):

    def test_the_purge_runs_before_the_teardown(self):
        """After the teardown there is no container left to reach the
        bucket, so this order is the whole design."""
        labels = self._labels(self._executor(self._job()))
        self.assertIn(PURGE_LABEL, labels)
        self.assertLess(
            labels.index(PURGE_LABEL), labels.index(_TEARDOWN_FIRST_STEP),
        )

    def test_a_failed_purge_aborts_the_teardown(self):
        """Without this the objects are stranded: the teardown destroys
        the only thing that could still delete them."""
        steps = self._executor(self._job()).get_commands()
        purge = next(s for s in steps if s[0] == PURGE_LABEL)
        self.assertEqual(len(purge), 3, "purge step carries no options")
        self.assertTrue(purge[2].get("stop_on_failure"))

    def test_keeping_the_record_does_not_purge(self):
        """Keeping the record is not a deletion — those backups still
        have an instance they belong to."""
        job = self._job(payload={"keep_in_panel": True})
        self.assertNotIn(PURGE_LABEL, self._labels(self._executor(job)))

    def test_an_instance_without_backups_has_no_purge_step(self):
        """No destination anywhere in the chain — the honest "nothing to
        purge" case.

        Clearing the instance's own field is not enough: the effective
        backend falls back to the project's and then to the global
        default, and a deployment that has one would still resolve a
        destination. All three have to be gone.
        """
        self.instance.backup_backend_id = False
        self.project.backup_backend_id = False
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.instance.invalidate_recordset()
        self.assertNotIn("backup", self.instance.expected_services())
        self.assertNotIn(
            PURGE_LABEL, self._labels(self._executor(self._job())),
        )

    def test_a_staging_instance_is_never_purged(self):
        """Only production renders the backup container.

        ``_backup_enabled()`` is true for staging as soon as a global
        default destination exists, so gating on that flag alone would
        send the purge after a container that was never deployed — the
        script would answer "service missing" and no staging instance
        could ever be deleted.
        """
        self.instance.environment = "staging"
        self.assertTrue(
            self.instance._backup_enabled(),
            "fixture is wrong: the flag must be true for this to prove "
            "anything",
        )
        self.assertNotIn("backup", self.instance.expected_services())
        self.assertNotIn(
            PURGE_LABEL, self._labels(self._executor(self._job())),
        )

    def test_the_gate_is_the_panel_not_the_host_s_answer(self):
        """``docker compose exec`` says `service "backup" is not running`
        both when it is stopped and when it does not exist, so the host
        can never tell "nothing to purge" from "could not reach it".
        The decision has to come from the panel."""
        with patch.object(
            type(self.instance), "expected_services",
            return_value=("odoo", "db"),
        ):
            self.assertNotIn(
                PURGE_LABEL, self._labels(self._executor(self._job())),
            )


class TestPurgeNeverTouchesALiveInstance(_PurgeBase):
    """The move cleanups reuse these teardown commands.

    Both run against a host the instance has left — the abandoned source
    copy after a move, or the half-built target of a rolled-back one —
    while the instance is alive somewhere else and those backups are
    still its own. Purging there would destroy the backups of a running
    instance, which is the worst thing this feature could do.
    """

    def test_move_cleanup_source_does_not_purge(self):
        job = self._job(code="move_cleanup_source")
        ex = self._executor(job, cls=MoveCleanupSourceExecutor)
        self.assertFalse(ex._owns_instance_lifecycle)
        self.assertNotIn(PURGE_LABEL, self._labels(ex))

    def test_move_rollback_cleanup_does_not_purge(self):
        job = self._job(code="move_rollback_cleanup")
        ex = self._executor(job, cls=MoveRollbackCleanupExecutor)
        self.assertFalse(ex._owns_instance_lifecycle)
        self.assertNotIn(PURGE_LABEL, self._labels(ex))

    def test_the_gate_is_the_lifecycle_flag(self):
        """Pinned explicitly: a future teardown-reusing executor that
        forgets to set the flag would inherit the purge."""
        ex = self._executor(self._job())
        self.assertTrue(ex._owns_instance_lifecycle)
        self.assertIn(PURGE_LABEL, self._labels(ex))


class TestPurgeOutcomes(_PurgeBase):

    def _results(self, purge_exit, teardown_exit=0):
        return {
            PURGE_LABEL: {"stdout": "", "exit_status": purge_exit},
            _TEARDOWN_FIRST_STEP: {
                "stdout": "", "exit_status": teardown_exit,
            },
        }

    def test_an_already_empty_prefix_is_not_a_failure(self):
        """The invariant already holds. Refusing to continue would make
        an instance whose bucket someone emptied by hand impossible to
        delete."""
        ex = self._executor(self._job())
        self.assertFalse(
            ex.parse_results(self._results(PURGE_EXIT_ALREADY_EMPTY)),
        )

    def test_every_other_non_zero_exit_fails_the_job(self):
        ex = self._executor(self._job())
        for code in PURGE_ALERT_BY_EXIT:
            with self.subTest(exit=code):
                self.assertTrue(ex.parse_results(self._results(code)))

    def test_a_failing_teardown_still_fails(self):
        """The tolerance is scoped to the purge label alone."""
        ex = self._executor(self._job())
        self.assertTrue(
            ex.parse_results(
                self._results(PURGE_EXIT_ALREADY_EMPTY, teardown_exit=1),
            ),
        )


class TestPurgeAlerts(_PurgeBase):

    def _active(self, code):
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", code),
            ("instance_id", "=", self.instance.id),
            ("state", "=", "active"),
        ])

    def test_each_exit_code_raises_its_own_alert(self):
        """One shared "delete failed" would send the operator to the job
        log every time; each of these has a different fix."""
        for code, (alert_code, _msg) in PURGE_ALERT_BY_EXIT.items():
            with self.subTest(exit=code):
                ex = self._executor(self._job())
                ex._alert_on_purge_failure(
                    {PURGE_LABEL: {"stdout": "", "exit_status": code}},
                    self.instance,
                )
                alert = self._active(alert_code)
                self.assertTrue(alert, f"exit {code} raised no alert")
                self.assertIn(self.instance.name, alert.message)

    def test_a_teardown_failure_raises_no_purge_alert(self):
        """The purge is not what failed, so it must not be blamed."""
        ex = self._executor(self._job())
        ex._alert_on_purge_failure(
            {_TEARDOWN_FIRST_STEP: {"stdout": "", "exit_status": 1}},
            self.instance,
        )
        for alert_code, _msg in PURGE_ALERT_BY_EXIT.values():
            self.assertFalse(self._active(alert_code))

    def test_the_alert_names_the_instance_and_carries_the_job(self):
        job = self._job()
        ex = self._executor(job)
        ex._alert_on_purge_failure(
            {PURGE_LABEL: {"stdout": "", "exit_status": 21}},
            self.instance,
        )
        alert = self._active("backup_purge_unauthorized")
        self.assertEqual(alert.job_id, job)
        self.assertEqual(alert.host_id, self.host)


class TestArchiveStep(_PurgeBase):
    """Keeping the record takes a copy instead of destroying them.

    The two paths are mirror images and share every gate: one leaves
    exactly one chain behind, the other leaves none, and neither ever
    leaves a chain without an instance that owns it.
    """

    def _archive_job(self):
        return self._job(payload={"keep_in_panel": True})

    def test_keeping_the_record_takes_the_archive_copy(self):
        labels = self._labels(self._executor(self._archive_job()))
        self.assertIn(ARCHIVE_LABEL, labels)
        self.assertNotIn(PURGE_LABEL, labels)

    def test_deleting_purges_and_does_not_archive(self):
        labels = self._labels(self._executor(self._job()))
        self.assertIn(PURGE_LABEL, labels)
        self.assertNotIn(ARCHIVE_LABEL, labels)

    def test_the_archive_runs_before_the_teardown(self):
        """``compose down -v`` destroys the container that holds
        duplicity, the credentials and the passphrase."""
        labels = self._labels(self._executor(self._archive_job()))
        self.assertLess(
            labels.index(ARCHIVE_LABEL), labels.index(_TEARDOWN_FIRST_STEP),
        )

    def test_a_failed_archive_aborts_the_teardown(self):
        """An archive that silently kept nothing is worse than one that
        refused: the record would advertise a copy that is not there."""
        steps = self._executor(self._archive_job()).get_commands()
        archive = next(s for s in steps if s[0] == ARCHIVE_LABEL)
        self.assertEqual(len(archive), 3)
        self.assertTrue(archive[2].get("stop_on_failure"))

    def test_a_staging_instance_is_never_archived(self):
        self.instance.environment = "staging"
        self.assertNotIn(
            ARCHIVE_LABEL, self._labels(self._executor(self._archive_job())),
        )

    def test_the_move_cleanups_never_archive(self):
        """Same gate as the purge: their instance is alive elsewhere."""
        job = self._job(code="move_cleanup_source",
                        payload={"keep_in_panel": True})
        ex = self._executor(job, cls=MoveCleanupSourceExecutor)
        self.assertNotIn(ARCHIVE_LABEL, self._labels(ex))

    def test_each_archive_exit_code_raises_its_own_alert(self):
        for code, (alert_code, _msg) in ARCHIVE_ALERT_BY_EXIT.items():
            with self.subTest(exit=code):
                ex = self._executor(self._archive_job())
                ex._alert_on_purge_failure(
                    {ARCHIVE_LABEL: {"stdout": "", "exit_status": code}},
                    self.instance,
                )
                alert = self.env["cloud.alert"].sudo().search([
                    ("code", "=", alert_code),
                    ("instance_id", "=", self.instance.id),
                    ("state", "=", "active"),
                ])
                self.assertTrue(alert, f"exit {code} raised no alert")


class TestFrozenBackupPath(_PurgeBase):
    """Archiving pins where the copy lives.

    The computed path derives from the project's remote folder and the
    instance name; once archived, that derivation can quietly become
    false — deleting a tenant detaches its project and the computed path
    falls back to a shared ``.../default/`` prefix. Everything that
    touches the copy afterwards must read the frozen value.
    """

    def test_archiving_freezes_the_computed_path(self):
        computed = self.instance.instance_backup_dst
        self.assertTrue(computed)
        self.instance._finalize_removal(keep_in_panel=True)
        self.assertEqual(self.instance.custom_backup_dst, computed)

    def test_the_frozen_path_survives_losing_the_project(self):
        """The exact drift this defends against."""
        self.instance._finalize_removal(keep_in_panel=True)
        frozen = self.instance.custom_backup_dst
        self.instance.project_id = False
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.instance_backup_dst, frozen)
        self.assertNotIn("/default/", self.instance.instance_backup_dst)

    def test_an_existing_override_is_never_overwritten(self):
        """An imported instance carries its real location there."""
        self.instance.custom_backup_dst = "boto3+s3://imported/elsewhere"
        self.instance._finalize_removal(keep_in_panel=True)
        self.assertEqual(
            self.instance.custom_backup_dst, "boto3+s3://imported/elsewhere",
        )

    def test_deleting_does_not_freeze_anything(self):
        """There is nothing left to point at."""
        self.instance._finalize_removal(keep_in_panel=False)
        self.assertFalse(self.instance.exists())
