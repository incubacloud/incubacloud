import base64
import os
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from odoo import fields

from .abstract_executor import AbstractSSHExecutor


class BackupCreateExecutor(AbstractSSHExecutor):
    """Create a manual backup.

    Production instances: triggers the duplicity jobrunner inside the
    backup container (full backup to S3).

    Non-production instances: uses click-odoo-backupdb inside the odoo
    container to create a ZIP (DB + filestore), then downloads it via
    SFTP and stores it as an ir.attachment with a 2-hour TTL.
    """

    _job_type = "backup_create"

    def _inst(self):
        return self.job.instance_id

    def _is_production(self):
        return self._inst().environment == 'production'

    def _remote_tmp(self):
        return f"/tmp/.incubacloud-backup-{self._inst().name}.zip"

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("backup_create job has no instance_id")
        if not inst.deployed:
            raise ValueError("Instance is not deployed.")

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)

        if self._is_production():
            return [
                (
                    "Create backup",
                    f"cd {d} && docker compose exec -T backup"
                    f" /etc/periodic/daily/jobrunner",
                ),
            ]

        # Non-production: click-odoo-backupdb inside the running container,
        # then `docker compose cp` the ZIP to the host for SFTP download.
        dbname = inst.postgres_dbname or 'prod'
        remote_tmp = self._remote_tmp()
        return [
            (
                "Create backup",
                f"cd {d} && docker compose exec -T odoo"
                f" click-odoo-backupdb {dbname} {remote_tmp}",
            ),
            (
                "Copy to host",
                f"cd {d} && docker compose cp"
                f" odoo:{remote_tmp} {remote_tmp}"
                f" && docker compose exec -T odoo rm -f {remote_tmp}",
            ),
        ]

    def parse_results(self, results):
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get('exit_status', 1) != 0
        ]

    async def after_commands(self, transport, results):
        if not self._is_production():
            await self._download_backup(transport)
            await transport.run(f"rm -f {self._remote_tmp()}")

    async def on_success(self, results):
        if self._is_production():
            self._sys("✓ Backup created successfully in S3.")

    async def _download_backup(self, transport):
        """Download the non-prod backup ZIP via SFTP."""
        inst = self._inst()
        remote_tmp = self._remote_tmp()
        ts = datetime.now().strftime('%Y%m%dT%H%M%S')
        filename = f"{inst.name}-backup-{ts}.zip"

        self._sys("Downloading backup to Odoo server…")
        local_fd, local_tmp = tempfile.mkstemp(
            prefix=".incubacloud-backup-", suffix=".zip",
        )
        os.close(local_fd)
        try:
            await transport.download_file(remote_tmp, local_tmp)

            self._sys("✓ Downloaded. Storing as attachment…")
            data = Path(local_tmp).read_bytes()

            with self.job.env.registry.cursor() as cr:
                env = self.job.env(cr=cr)
                att = env['ir.attachment'].create({
                    'name': filename,
                    'type': 'binary',
                    'datas': base64.b64encode(data).decode('ascii'),
                    'res_model': 'cloud.job',
                    'res_id': self.job.id,
                })
                env['cloud.instance.backup'].create({
                    'instance_id': inst.id,
                    'backup_type': 'Full',
                    'backup_time': fields.Datetime.now(),
                    'attachment_id': att.id,
                })

            self._sys(
                "✓ Backup ready for download (expires in 2 hours)."
            )
        finally:
            with suppress(OSError):
                Path(local_tmp).unlink()

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
