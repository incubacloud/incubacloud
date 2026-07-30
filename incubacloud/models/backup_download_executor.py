import base64
import os
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from .abstract_executor import AbstractSSHExecutor, validate_dup_time


class BackupDownloadExecutor(AbstractSSHExecutor):
    """Download an exact-state backup as a ZIP file.

    Production: restore the requested timestamp from duplicity/S3 in
    the ``backup`` container, then ``cp`` it out and zip it.

    Non-production: there is no historical snapshot store, so the only
    supported time is ``'latest'`` and the executor takes a live dump of
    the current DB via ``click-odoo-backupdb`` running in the ``odoo``
    container (with ``/tmp`` bind-mounted so the ZIP lands directly on
    the host — same pattern as ``BackupCreateExecutor``).

    Payload:
        time: duplicity timestamp (prod) or 'latest' (both)
        download_type: 'dump' (SQL only) or 'all' (DB + filestore)
    """

    _job_type = "backup_download"

    def _inst(self):
        return self.job.instance_id

    def _tmp_dir(self):
        return f"/tmp/.incubacloud-bkdl-{self._inst().name}"

    def _tmp_archive(self):
        return f"/tmp/.incubacloud-bkdl-{self._inst().name}.zip"

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("backup_download job has no instance_id")
        payload = self.job.payload or {}
        if not payload.get('time'):
            raise ValueError("Missing 'time' in job payload.")
        # 'latest' means no --time flag → duplicity uses the most recent.
        # Validate the format up-front: the value flows verbatim into a
        # shell command (``--time "<value>"``) so it must be free of
        # quotes, whitespace and shell metacharacters.
        validate_dup_time(payload['time'])
        # Non-prod has no historical snapshots — only ``latest`` (= a
        # fresh live dump on demand) is meaningful there.  Reject early
        # so the SPA can surface a clear error instead of bombing on
        # SSH with "service backup is not running".
        if inst.environment != 'production' and payload['time'] != 'latest':
            raise ValueError(
                "Non-production instances do not retain historical"
                " backups; only time='latest' (a live dump of the"
                " current state) is supported."
            )

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        payload = self.job.payload or {}
        raw_time = payload['time']
        mode = payload.get('download_type', 'dump')
        dbname = inst.postgres_dbname or 'prod'
        archive = self._tmp_archive()

        # Non-prod: live dump via click-odoo-backupdb directly into the
        # host's /tmp (one step, no separate ``cp`` needed).  Same
        # binary handles both filestore modes via --filestore /
        # --no-filestore so we keep a single non-prod codepath.  The
        # binary writes a ZIP that already matches the layout expected
        # by ``_download_zip``.
        if inst.environment != 'production':
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
        await self._download_zip(transport)

    async def _download_zip(self, transport):
        """Download the packaged ZIP via SFTP → ir.attachment."""
        inst = self._inst()
        archive = self._tmp_archive()
        payload = self.job.payload or {}
        raw = payload.get('time', 'unknown')
        time_label = (
            datetime.now().strftime('%Y%m%dT%H%M%S')
            if raw == 'latest' else raw
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
            data = Path(local_tmp).read_bytes()

            with self.job.env.registry.cursor() as cr:
                env = self.job.env(cr=cr)
                env['ir.attachment'].create({
                    'name': filename,
                    'type': 'binary',
                    'datas': base64.b64encode(data).decode('ascii'),
                    'res_model': 'cloud.job',
                    'res_id': self.job.id,
                })
        finally:
            with suppress(OSError):
                Path(local_tmp).unlink()

        # Cleanup remote temp file
        await transport.run(f"rm -f {archive}")

    async def on_success(self, results):
        self._sys(
            "✓ Backup ready for download (expires in 2 hours)."
        )

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
