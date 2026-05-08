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

        # Non-production: click-odoo-backupdb in a transient ``run --rm``
        # container with /tmp bind-mounted, so the ZIP lands directly on
        # the host at ``remote_tmp`` and a separate ``docker compose cp``
        # step is unnecessary. ``run --rm`` (as opposed to ``exec``) also
        # works when the odoo service is stopped — e.g. after a failed
        # restore left the instance in error state but the operator still
        # wants to grab a backup of the current DB before iterating.
        # ``with_filestore`` is forced through ``bool()`` because the
        # value reaches a shell command via the ``--filestore`` /
        # ``--no-filestore`` flag — never let an arbitrary string flow
        # into the binary invocation.
        with_filestore = bool(
            (self.job.payload or {}).get('with_filestore', True)
        )
        filestore_flag = '--filestore' if with_filestore else '--no-filestore'
        dbname = inst.postgres_dbname or 'prod'
        remote_tmp = self._remote_tmp()
        remote_filename = os.path.basename(remote_tmp)
        return [
            (
                "Create backup",
                f"cd {d} && docker compose run --rm"
                f" -v /tmp:/host-tmp"
                f" odoo click-odoo-backupdb"
                f" {filestore_flag} {dbname} /host-tmp/{remote_filename}",
                {"stop_on_failure": True},
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
            size = len(data)
            with_filestore = bool(
                (self.job.payload or {}).get('with_filestore', True)
            )

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
                    'with_filestore': with_filestore,
                    'size': size,
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
