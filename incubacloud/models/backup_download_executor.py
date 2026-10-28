import os
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from ._odoo_versions import script_env
from .abstract_executor import (
    AbstractSSHExecutor,
    handoff_dir,
    handoff_sweep,
    validate_dup_time,
)


class BackupDownloadExecutor(AbstractSSHExecutor):
    """Download an exact-state backup as a ZIP file.

    Production: restore the requested timestamp from duplicity/S3 in
    the ``backup`` container, then ``cp`` it out and zip it. Asking for
    ``time='live'`` instead takes an on-demand dump of the current DB —
    the "data as of this second" option, at the cost of a pg_dump on a
    running production, and the only path available when the instance
    has no backup backend configured.

    Non-production: there is no historical snapshot store, so the only
    supported times are ``'latest'`` and ``'live'`` (synonyms there) and
    the executor takes a live dump of the current DB via
    ``click-odoo-backupdb`` running in the ``odoo`` container (with the
    job's drop directory bind-mounted so the ZIP lands directly on the
    host — same pattern as ``BackupCreateExecutor``).

    Payload:
        time: duplicity timestamp, 'latest', or 'live' (on-demand dump)
        download_type: 'dump' (SQL only) or 'all' (DB + filestore)
        handoff: 'host' to leave the ZIP on the host for the next job
            in the chain (``restore_instance`` mode ``from_host``)
            instead of downloading it into an ``ir.attachment``. The
            attachment relay round-trips the whole archive through the
            core's RAM (base64) and database; the handoff never moves
            it at all. Absent = the attachment behaviour, which remains
            the right one when a *person* is the consumer (the job's
            download button in the SPA).
    """

    _job_type = "backup_download"

    def _inst(self):
        return self.job.instance_id

    def _handoff(self):
        """Whether this job stages the ZIP on the host for a chained
        restore instead of attaching it (payload ``handoff='host'``)."""
        return (self.job.payload or {}).get('handoff') == 'host'

    def _live(self):
        """Whether this download is a dump of the database as it runs.

        :rtype: bool
        """
        return (
            self._inst().environment != 'production'
            or (self.job.payload or {}).get('time') == 'live'
        )

    def _tmp_dir(self):
        """Return where a restored backup is packaged: the job's directory.

        :rtype: str
        """
        return self._job_path("bkdl")

    def _tmp_archive(self):
        """Return where the archive is built: in the job's own directory.

        Its drop directory when a container writes it. A handoff's too:
        only at the end is it handed over (``_handover_step``).

        :rtype: str
        """
        if self._live():
            return f"{self._drop_dir()}/backup.zip"
        return self._job_path("backup.zip")

    def _handover_step(self):
        """Return the step that hands a handoff's archive over, or ().

        Into :func:`handoff_dir`, which the restore recomputes from this
        job's id and which outlives this job's own directory. Last, and
        stopping the run when it fails: a restore must never find an
        archive that was not handed over whole.

        :rtype: tuple
        """
        if not self._handoff():
            return ()
        return ((
            "Hand the archive over",
            self.run_script(
                "backup_download.sh",
                [
                    "handover", self._inst_dir(self._inst()),
                    self._tmp_archive(), handoff_dir(self.env, self.job.id),
                ],
            ),
            {"stop_on_failure": True},
        ),)

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("backup_download job has no instance_id")
        payload = self.job.payload or {}
        if payload.get('handoff') not in (None, 'host'):
            raise ValueError(
                f"Invalid 'handoff' value: {payload['handoff']!r}."
                " Expected 'host' or absent."
            )
        if not payload.get('time'):
            raise ValueError("Missing 'time' in job payload.")
        # 'latest' means no --time flag → duplicity uses the most recent.
        # Validate the format up-front: the value flows verbatim into a
        # shell command (``--time "<value>"``) so it must be free of
        # quotes, whitespace and shell metacharacters.
        validate_dup_time(payload['time'])
        # Non-prod has no historical snapshots — only ``latest`` and its
        # explicit synonym ``live`` (= a fresh dump on demand) are
        # meaningful there.  Reject early so the SPA can surface a clear
        # error instead of bombing on SSH with "service backup is not
        # running".
        if (
            inst.environment != 'production'
            and payload['time'] not in ('latest', 'live')
        ):
            raise ValueError(
                "Non-production instances do not retain historical"
                " backups; only time='latest' or time='live' (a dump of"
                " the current state) is supported."
            )
        if self._handoff():
            # What chains that never reached their restore left: a day
            # old, this account's, best effort.
            with suppress(Exception):
                await transport.run(handoff_sweep(self.env, self.job.host_id))
        if self._live():
            # The dump's container writes there.
            await self._ensure_drop_dir(transport)
        else:
            # Restored and packaged in the job's directory.
            await self._ensure_job_dir(transport)

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        payload = self.job.payload or {}
        raw_time = payload['time']
        mode = payload.get('download_type', 'dump')
        dbname = inst.postgres_dbname or 'prod'
        archive = self._tmp_archive()

        # Live dump via click-odoo-backupdb directly into the job's drop
        # directory (one step, no separate ``cp`` needed).  Same binary
        # handles both filestore modes via --filestore / --no-filestore
        # so we keep a single codepath.  The binary writes a ZIP that
        # already matches the layout expected by ``_download_zip``.
        #
        # Taken for every non-prod instance (no snapshot store exists)
        # and for production when the caller explicitly asks for
        # ``time='live'`` — the duplicity path below is untouched.
        if inst.environment != 'production' or raw_time == 'live':
            return [
                (
                    "Create live backup",
                    self.run_script(
                        "backup_download.sh",
                        [
                            "live-dump", d, dbname, archive,
                            "all" if mode == 'all' else "db",
                        ],
                        env=script_env(inst.odoo_version),
                    ),
                    {"stop_on_failure": True},
                ),
                *self._handover_step(),
            ]

        tmp_dir = self._tmp_dir()

        # Production: restore inside the backup container, then copy out.
        if mode == 'dump':
            return [
                (
                    "Restore SQL from backup",
                    self.run_script(
                        "backup_download.sh",
                        ["restore-sql", d, dbname, raw_time],
                    ),
                ),
                (
                    "Extract and package",
                    self.run_script(
                        "backup_download.sh",
                        ["package-sql", d, dbname, tmp_dir, archive],
                    ),
                ),
                *self._handover_step(),
            ]

        # mode == 'all': full restore (DB + filestore)
        return [
            (
                "Restore full from backup",
                self.run_script(
                    "backup_download.sh", ["restore-full", d, raw_time],
                ),
            ),
            (
                "Extract and package",
                self.run_script(
                    "backup_download.sh",
                    ["package-full", d, dbname, tmp_dir, archive],
                ),
            ),
            *self._handover_step(),
        ]

    def parse_results(self, results):
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get('exit_status', 1) != 0
        ]

    async def after_commands(self, transport, results):
        if self._handoff():
            if not self._handoff_awaited():
                # Its restore was cancelled while this ran: the removal
                # that cancellation queued found nothing to remove yet.
                # Best effort, like the sweep: the archive is already
                # on the host, and a connection lost right here must not
                # fail a job that did its work; the day's sweep takes it.
                removed = False
                with suppress(Exception):
                    await transport.run(
                        f"rm -rf {handoff_dir(self.env, self.job.id)}"
                    )
                    removed = True
                self._sys(
                    "The restore this backup was for will not run: the "
                    "backup was removed from the host."
                    if removed else
                    "The restore this backup was for will not run: the "
                    "backup could not be removed from the host now; the "
                    "day's sweep takes it."
                )
                return
            # The chained restore consumes (and removes) the archive
            # where it lies; nothing crosses the wire here. Its path is
            # not written to the log: no other account should learn it.
            self._sys("✓ Backup handed over on the host for the next job.")
            return
        await self._download_zip(transport)

    def _handoff_awaited(self):
        """Whether a restore may still come for this job's handoff.

        Read on a cursor of its own: the restore is cancelled in another
        transaction, after this job's started. Only the restores of this
        job's chain that name it count; when there are none — a handoff
        asked for outside a chain — there is no telling, and the
        day-old sweep decides.

        :rtype: bool
        """
        with self._durable_env() as env:
            job = env["cloud.job"].sudo().browse(self.job.id).exists()
            if not job.queue_chain_key:
                return True
            restores = job.search([
                ("queue_chain_key", "=", job.queue_chain_key),
                ("job_type_id.code", "=", "restore_instance"),
            ]).filtered(lambda r: str(
                (r.payload or {}).get("source_job_id")
            ) == str(job.id))
            if not restores:
                return True
            return any(
                r.state in job._active_states for r in restores
            )

    async def _download_zip(self, transport):
        """Download the packaged ZIP via SFTP → ir.attachment."""
        inst = self._inst()
        archive = self._tmp_archive()
        payload = self.job.payload or {}
        raw = payload.get('time', 'unknown')
        # 'latest' and 'live' name a moment rather than a timestamp, so
        # the filename gets the wall clock instead of the literal word.
        time_label = (
            datetime.now().strftime('%Y%m%dT%H%M%S')
            if raw in ('latest', 'live') else raw
        )
        mode = payload.get('download_type', 'dump')
        suffix = 'full' if mode == 'all' else 'dump'
        filename = f"{inst.name}-backup-{time_label}-{suffix}.zip"

        self._sys("Downloading backup to Odoo server…")
        local_fd, local_tmp = tempfile.mkstemp(
            prefix=".incubacloud-bkdl-", suffix=".zip",
        )
        os.close(local_fd)
        try:
            await self._check_regular_file(transport, archive)
            await transport.download_file(archive, local_tmp)

            self._sys("✓ Downloaded. Storing as attachment…")
            # ``raw`` takes the bytes straight to the filestore; going
            # through ``datas`` would hold a second, base64 copy of the
            # whole archive in RAM for nothing.
            data = Path(local_tmp).read_bytes()

            with self._durable_env() as env:
                env['ir.attachment'].create({
                    'name': filename,
                    'type': 'binary',
                    'raw': data,
                    'res_model': 'cloud.job',
                    'res_id': self.job.id,
                })
        finally:
            with suppress(OSError):
                Path(local_tmp).unlink()

        # Cleanup remote temp file
        await transport.run(f"rm -f {archive}")

    async def on_success(self, results):
        if self._handoff():
            self._sys("✓ Backup ready for the chained restore.")
            return
        self._sys(
            "✓ Backup ready for download (expires in 2 hours)."
        )

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
