import os
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from .abstract_executor import (
    AbstractSSHExecutor,
    handoff_archive_path,
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
    ``click-odoo-backupdb`` running in the ``odoo`` container (with
    ``/tmp`` bind-mounted so the ZIP lands directly on the host — same
    pattern as ``BackupCreateExecutor``).

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

    def _tmp_dir(self):
        return f"/tmp/.incubacloud-bkdl-{self._inst().name}"

    def _tmp_archive(self):
        # Handoff archives are keyed by job id — the consumer recomputes
        # the path from ``source_job_id`` alone, and a concurrent user
        # download on the same instance can never overwrite them.
        if self._handoff():
            return handoff_archive_path(self.job.id)
        return f"/tmp/.incubacloud-bkdl-{self._inst().name}.zip"

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

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        payload = self.job.payload or {}
        raw_time = payload['time']
        mode = payload.get('download_type', 'dump')
        dbname = inst.postgres_dbname or 'prod'
        archive = self._tmp_archive()

        # Live dump via click-odoo-backupdb directly into the host's
        # /tmp (one step, no separate ``cp`` needed).  Same binary
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
                    ),
                    {"stop_on_failure": True},
                ),
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
        ]

    def parse_results(self, results):
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get('exit_status', 1) != 0
        ]

    async def after_commands(self, transport, results):
        if self._handoff():
            # The chained restore consumes (and removes) the archive
            # where it lies; nothing crosses the wire here.
            self._sys(
                "✓ Backup staged on the host for the next job "
                f"({self._tmp_archive()})."
            )
            return
        await self._download_zip(transport)

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
            await transport.download_file(archive, local_tmp)

            self._sys("✓ Downloaded. Storing as attachment…")
            # ``raw`` takes the bytes straight to the filestore; going
            # through ``datas`` would hold a second, base64 copy of the
            # whole archive in RAM for nothing.
            data = Path(local_tmp).read_bytes()

            with self.job.env.registry.cursor() as cr:
                env = self.job.env(cr=cr)
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
