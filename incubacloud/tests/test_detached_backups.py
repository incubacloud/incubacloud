"""The copies and downloads run their long steps apart on their server.

Phase 3 (12-nov-2026): the dump, duplicity's restore and the packaging
of a copy or a download run apart, as the restores' long steps do. What
follows them fetches the archive through the panel (SFTP, then an
attachment): it cannot run apart, so an attempt that ran long leaves it
to a fresh one (``_outcome_needs_room``). A download handed over on its
server for a chained restore fetches nothing, and a copy of a production
goes to the backup store. A neutralized download that is stopped drops
its throwaway database in the job's own directory, the one its checkpoint
names, whoever closes it.
"""
import asyncio
import contextlib
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from ..models.abstract_executor import JobCancelled
from ..models.backup_create_executor import BackupCreateExecutor
from ..models.backup_download_executor import BackupDownloadExecutor
from ..models.backup_download_neutralized_executor import (
    BackupDownloadNeutralizedExecutor,
)
from ..models.transport import CommandResult, SSHTransport

#: A job directory pinned for the tests: it is random in a run.
_JOB_DIR = "/tmp/.incubacloud-job-00112233aabbccdd"


class _BackupCase(TransactionCase):
    """A host with a deployed production and a deployed staging."""

    def setUp(self):
        """The host and the two instances."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "apart-backups", "ip_address": "192.0.2.97",
            "user": "ubuntu", "wildcard_domain": "apart-backups.example.com",
        })
        project = self.env["cloud.project"].create({"name": "apart-backups"})
        self.prod, self.staging = self.env["cloud.instance"].create([
            {
                "name": f"apart-{environment}", "project_id": project.id,
                "environment": environment, "host_id": self.host.id,
                "state": "deployed", "odoo_version": "19.0",
            }
            for environment in ("production", "staging")
        ])

    def _executor(self, cls, inst, payload):
        """An executor of *cls* for a job of its type on *inst*.

        :rtype: AbstractExecutor
        """
        job = self.env["cloud.job"].sudo().create({
            "name": cls._job_type, "host_id": self.host.id,
            "instance_id": inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", cls._job_type)], limit=1,
            ).id,
            "payload": payload,
            "queue_job_uuid": f"apart-{cls._job_type}-{inst.id}",
        })
        executor = cls(job, self.host)
        executor._job_dir_path = _JOB_DIR
        return executor

    @staticmethod
    def _apart(commands):
        """Return ``{label: options}`` of the steps marked ``detached``."""
        return {
            step[0]: step[2] for step in commands
            if len(step) == 3 and step[2].get("detached")
        }


class TestTheStepsApart(_BackupCase):
    """Which steps of each copy and download run apart, with their options."""

    def test_a_copy(self):
        """The dump of a staging, and the backup store's run of a production."""
        staging = self._executor(BackupCreateExecutor, self.staging, {})
        self.assertEqual(self._apart(staging.get_commands()), {
            "Create backup": {"stop_on_failure": True, "detached": True},
        })
        prod = self._executor(BackupCreateExecutor, self.prod, {})
        self.assertEqual(self._apart(prod.get_commands()), {
            "Create backup": {"detached": True},
        })

    def test_a_download(self):
        """The dump, the restore from the store and the packaging; never
        the handover, a ``mv``."""
        live = self._executor(
            BackupDownloadExecutor, self.staging,
            {"time": "live", "download_type": "all", "handoff": "host"},
        )
        self.assertEqual(self._apart(live.get_commands()), {
            "Create live backup": {"stop_on_failure": True, "detached": True},
        })
        handover = live.get_commands()[-1]
        self.assertEqual(handover[0], "Hand the archive over")
        self.assertNotIn("detached", handover[2])
        for mode, restore in (("dump", "Restore SQL from backup"),
                              ("all", "Restore full from backup")):
            stored = self._executor(
                BackupDownloadExecutor, self.prod,
                {"time": "latest", "download_type": mode},
            )
            self.assertEqual(self._apart(stored.get_commands()), {
                restore: {"detached": True},
                "Extract and package": {"detached": True},
            }, mode)

    def test_a_neutralized_download(self):
        """The source, its neutralization and the dump; not the cleanup."""
        for inst, time_, source in (
            (self.prod, "latest", "Restore source dump from S3"),
            (self.staging, "live", "Dump live database"),
        ):
            for with_filestore, dump in (
                (True, "Create neutralized backup (DB + filestore)"),
                (False, "Create neutralized backup (SQL only)"),
            ):
                executor = self._executor(
                    BackupDownloadNeutralizedExecutor, inst,
                    {"time": time_, "with_filestore": with_filestore},
                )
                apart = {"stop_on_failure": True, "detached": True}
                self.assertEqual(self._apart(executor.get_commands()), {
                    source: apart,
                    "Restore and neutralize in temp DB": apart,
                    dump: apart,
                }, (inst.environment, with_filestore))


class TestTheFetchGetsAFreshAttempt(_BackupCase):
    """An attempt that ran long leaves the fetch through the panel to a
    fresh one; nothing else asks for room."""

    def _ran(self, executor, elapsed_s=200, limit_s=900):
        """Make *executor* an attempt with a checkpoint that ran *elapsed_s*.

        :return: whether it would leave its outcome to a fresh attempt
        :rtype: bool
        """
        executor._checkpoint = True
        executor._attempt_limit = limit_s
        executor._attempt_started = time.monotonic() - elapsed_s
        return executor._outcome_wants_fresh_attempt(1)

    def test_the_downloads_to_the_panel_ask_for_room(self):
        """A staging's copy, a download and a neutralized download."""
        for cls, inst, payload in (
            (BackupCreateExecutor, self.staging, {}),
            (BackupDownloadExecutor, self.prod,
             {"time": "latest", "download_type": "dump"}),
            (BackupDownloadNeutralizedExecutor, self.staging, {"time": "live"}),
        ):
            executor = self._executor(cls, inst, payload)
            self.assertTrue(executor._outcome_needs_room, cls.__name__)
            self.assertTrue(self._ran(executor), cls.__name__)
            # Short, or with most of the request left: it fetches now.
            self.assertFalse(self._ran(executor, elapsed_s=30), cls.__name__)
            self.assertFalse(
                self._ran(executor, elapsed_s=100, limit_s=3600), cls.__name__,
            )

    def test_the_attempt_that_resumed_at_the_fetch_does_not_park_again(self):
        """It is the fresh one."""
        executor = self._executor(BackupCreateExecutor, self.staging, {})
        executor._resumed = True
        executor._resume_index = 1
        self.assertFalse(self._ran(executor))

    def test_what_fetches_nothing_through_the_panel_asks_none(self):
        """A production's copy goes to the store; a handoff stays on its
        server for the restore."""
        for cls, inst, payload in (
            (BackupCreateExecutor, self.prod, {}),
            (BackupDownloadExecutor, self.staging,
             {"time": "live", "download_type": "dump", "handoff": "host"}),
        ):
            executor = self._executor(cls, inst, payload)
            self.assertFalse(executor._outcome_needs_room, cls.__name__)
            self.assertFalse(self._ran(executor), cls.__name__)


class TestAStoppedNeutralizedDownloadDropsItsDatabase(_BackupCase):
    """Closed from its checkpoint, it cleans in the checkpoint's directory."""

    def test_the_stop_drops_the_temp_db_and_the_job_s_files(self):
        """The executor that closes it is a new one: the directory is the
        one the job ran in, not a new random one."""
        executor = self._executor(
            BackupDownloadNeutralizedExecutor, self.prod, {"time": "latest"},
        )
        apart = {"stop_on_failure": True, "detached": True}
        row = self.env["cloud.job.checkpoint"].sudo().create({
            "job_id": executor.job.id, "host_id": self.host.id,
            "next_index": 1, "detached_started": True, "parked": True,
            "queue_job_uuid": executor.job.queue_job_uuid,
            "data": json.dumps({
                "plan": [
                    ["Restore source dump from S3", "src", apart],
                    ["Restore and neutralize in temp DB", "neutralize", apart],
                ],
                "results": {
                    "Restore source dump from S3": {"stdout": "", "exit_status": 0},
                },
                "job_dir": _JOB_DIR, "private_uploads": [], "carried": {},
            }),
        })
        closing = BackupDownloadNeutralizedExecutor(executor.job, self.host)
        closing._adopt_checkpoint(row)
        ran = []

        @contextlib.asynccontextmanager
        async def connect(_host):
            """A connection that records what it is sent and says 0."""
            transport = MagicMock(spec=SSHTransport)

            async def answer(command, *_args, **_kwargs):
                """Record *command*; answer as a command that worked."""
                ran.append(command)
                return CommandResult("", 0)

            transport.run.side_effect = answer
            yield transport

        Host = type(self.env["cloud.host"])
        with patch.object(
            Host, "get_transport", autospec=True, side_effect=connect,
        ), patch.object(BackupDownloadNeutralizedExecutor, "_sys", autospec=True):
            asyncio.run(closing.on_cancelled_running(
                dict(closing._checkpoint_data["results"]), JobCancelled("stop"),
            ))
        self.assertEqual(len(ran), 1, ran)
        self.assertIn(f"dropdb --if-exists __ic_neutral_{executor.job.id}", ran[0])
        self.assertIn(
            f"rm -rf {_JOB_DIR}/bkneu {_JOB_DIR}/neutralized.zip", ran[0],
        )


class TestTheFreshAttemptFetchesWhatTheFirstMade(_BackupCase):
    """The attempt that resumes at the fetch runs neither ``before_execute``
    nor ``get_commands``: what ``after_commands`` and ``on_success`` read
    comes from the job's payload, its instance and the checkpoint's
    directory, none of it set before the steps (no ``_carried``)."""

    def _resumed_at_the_fetch(self, cls, inst, payload):
        """An attempt of *cls* that resumed a job at its fetch.

        :return: the executor, and the paths its fetch read and fetched
        :rtype: tuple
        """
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        first = self._executor(cls, inst, payload)
        plan = [
            [step[0], step[1], step[2] if len(step) == 3 else {}]
            for step in first.get_commands()
        ]
        row = self.env["cloud.job.checkpoint"].sudo().create({
            "job_id": first.job.id, "host_id": self.host.id,
            "next_index": len(plan), "parked": True,
            "queue_job_uuid": first.job.queue_job_uuid,
            "data": json.dumps({
                "plan": plan,
                "results": {
                    step[0]: {"stdout": "", "exit_status": 0} for step in plan
                },
                "job_dir": _JOB_DIR, "private_uploads": [], "carried": {},
            }),
        })
        fresh = cls(first.job, self.host)
        fresh._adopt_checkpoint(row)
        self.assertGreaterEqual(fresh._resume_index, len(plan))
        fetched = []
        transport = MagicMock(spec=SSHTransport)

        async def run(command, *_args, **_kwargs):
            """Answer every check as a regular file that is there."""
            return CommandResult("", 0)

        async def download(remote, local):
            """Record what is fetched; write a small archive."""
            fetched.append(remote)
            Path(local).write_bytes(b"zip")

        transport.run.side_effect = run
        transport.download_file.side_effect = download
        lines = []
        with patch.object(
            cls, "_sys", autospec=True,
            side_effect=lambda _self, line: lines.append(line),
        ):
            asyncio.run(fresh.after_commands(transport, row._data()["results"]))
            asyncio.run(fresh.on_success(row._data()["results"]))
        return fresh, fetched, lines

    def _attachments(self, job):
        """The archives attached to *job*."""
        return self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "cloud.job"), ("res_id", "=", job.id),
        ])

    def test_a_staging_s_copy(self):
        """Its dump, from the drop directory of the first attempt."""
        fresh, fetched, lines = self._resumed_at_the_fetch(
            BackupCreateExecutor, self.staging, {"with_filestore": False},
        )
        self.assertEqual(fetched, [f"{_JOB_DIR}/out/backup.zip"])
        self.assertEqual(len(self._attachments(fresh.job)), 1)
        backup = self.env["cloud.instance.backup"].sudo().search(
            [("instance_id", "=", self.staging.id)],
        )
        self.assertFalse(backup.with_filestore)
        self.assertTrue(any("ready for download" in line for line in lines))

    def test_a_download(self):
        """Packaged from the store, in the first attempt's directory."""
        fresh, fetched, _lines = self._resumed_at_the_fetch(
            BackupDownloadExecutor, self.prod,
            {"time": "latest", "download_type": "all"},
        )
        self.assertEqual(fetched, [f"{_JOB_DIR}/backup.zip"])
        attachment = self._attachments(fresh.job)
        self.assertEqual(len(attachment), 1)
        self.assertTrue(attachment.name.endswith("-full.zip"), attachment.name)

    def test_a_neutralized_download(self):
        """Its archive, from the first attempt's directory."""
        fresh, fetched, _lines = self._resumed_at_the_fetch(
            BackupDownloadNeutralizedExecutor, self.staging,
            {"time": "live", "with_filestore": True},
        )
        self.assertEqual(fetched, [f"{_JOB_DIR}/neutralized.zip"])
        attachment = self._attachments(fresh.job)
        self.assertTrue(attachment.name.endswith("-full.zip"), attachment.name)
