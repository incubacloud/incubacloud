import base64
import os
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from .abstract_executor import AbstractSSHExecutor, validate_dup_time


class BackupDownloadExecutor(AbstractSSHExecutor):
    """Download a specific backup from duplicity as a ZIP file.

    Only used for production instances. Non-production backups are
    already stored as ir.attachments and can be downloaded directly.

    Payload:
        time: duplicity timestamp (e.g. '2026-03-19T02:00:00')
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

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        payload = self.job.payload or {}
        raw_time = payload['time']
        time_flag = (
            '' if raw_time == 'latest'
            else f" --time \"{raw_time}\""
        )
        mode = payload.get('download_type', 'dump')
        dbname = inst.postgres_dbname or 'prod'
        tmp_dir = self._tmp_dir()
        archive = self._tmp_archive()

        # Restore inside the backup container, then copy out.
        ctr_tmp = '/tmp/bkdl'
        if mode == 'dump':
            return [
                (
                    "Restore SQL from backup",
                    f"cd {d} && docker compose exec -T backup"
                    f" sh -c 'rm -rf {ctr_tmp} && mkdir -p {ctr_tmp}"
                    f" && dup restore --force"
                    f" {time_flag}"
                    f" --path-to-restore {dbname}.sql"
                    f" \"$DST\" {ctr_tmp}/{dbname}.sql'",
                ),
                (
                    "Extract and package",
                    f"cd {d}"
                    f" && mkdir -p {tmp_dir}"
                    f" && docker compose cp"
                    f" backup:{ctr_tmp}/{dbname}.sql"
                    f" {tmp_dir}/dump.sql"
                    f" && docker compose exec -T backup"
                    f" rm -rf {ctr_tmp}"
                    f" && cd {tmp_dir} && zip -r {archive} dump.sql"
                    f" && rm -rf {tmp_dir}",
                ),
            ]

        # mode == 'all': full restore (DB + filestore)
        return [
            (
                "Restore full from backup",
                f"cd {d} && docker compose exec -T backup"
                f" sh -c 'rm -rf {ctr_tmp} && mkdir -p {ctr_tmp}"
                f" && dup restore --force"
                f" {time_flag}"
                f" \"$DST\" {ctr_tmp}/'",
            ),
            (
                "Extract and package",
                f"cd {d}"
                f" && mkdir -p {tmp_dir}"
                f" && docker compose cp"
                f" backup:{ctr_tmp}/."
                f" {tmp_dir}/"
                f" && docker compose exec -T backup"
                f" rm -rf {ctr_tmp}"
                f" && cd {tmp_dir}"
                f" && mv {dbname}.sql dump.sql"
                f" && (cp -R odoo/filestore/{dbname} filestore"
                f" 2>/dev/null || true)"
                f" && zip -r {archive} dump.sql filestore"
                f" && rm -rf {tmp_dir}",
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
