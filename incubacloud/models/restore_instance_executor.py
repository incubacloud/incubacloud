import base64
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from .abstract_executor import AbstractSSHExecutor


class RestoreInstanceExecutor(AbstractSSHExecutor):
    """Restore a doodba instance database from a .zip backup."""

    _job_type = "restore_instance"

    def _inst(self):
        return self.job.instance_id

    def _remote_path(self):
        return f"/tmp/incubacloud-restore-{self._inst().id}.zip"

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def before_execute(self, transport):
        payload = self.job.payload or {}
        mode = payload.get('mode')

        if mode == 'browser':
            local_path = payload.get('local_path')
            if not local_path:
                raise ValueError(
                    "Backup file not found on Odoo server. "
                    "Please re-upload and try again."
                )
            # Defense-in-depth: ``local_path`` must point to a file the
            # /cloud/instance/<id>/restore controller created via
            # ``tempfile.mkstemp(prefix='cloud_restore_<inst_id>_',
            # suffix='.zip')``. Anything else (e.g. ``/etc/odoo/odoo.conf``
            # passed directly via JSON-RPC) is rejected — the executor
            # would otherwise both upload and unlink the target file.
            # ``resolve()`` canonicalises symlinks and ``..`` traversal.
            expected_prefix = (
                f"{tempfile.gettempdir()}/"
                f"cloud_restore_{self._inst().id}_"
            )
            resolved = str(Path(local_path).resolve())
            if not resolved.startswith(expected_prefix):
                raise ValueError(
                    "local_path must be a temp file created by the "
                    "upload controller (expected prefix %r, got %r)."
                    % (expected_prefix, resolved)
                )
            if not Path(resolved).exists():
                raise ValueError(
                    "Backup file not found on Odoo server. "
                    "Please re-upload and try again."
                )
            self._sys("Uploading backup to remote host via SFTP...")
            await transport.upload_file(resolved, self._remote_path())
            self._sys("✓ Backup transferred to remote host.")
            with suppress(Exception):
                Path(resolved).unlink()

        elif mode == 'from_job':
            source_job_id = payload.get('source_job_id')
            if not source_job_id:
                raise ValueError("Missing source_job_id in payload")
            att = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'cloud.job'),
                ('res_id', '=', int(source_job_id)),
            ], limit=1, order='id desc')
            if not att or not att.datas:
                raise ValueError(
                    "Backup attachment not found from source job"
                )
            self._sys("Retrieving backup from previous job…")
            data = base64.b64decode(att.datas)
            fd, local_path = tempfile.mkstemp(
                prefix=".incubacloud-restore-", suffix=".zip",
            )
            os.close(fd)
            Path(local_path).write_bytes(data)
            self._sys("Uploading backup to remote host via SFTP…")
            await transport.upload_file(local_path, self._remote_path())
            self._sys("✓ Backup transferred to remote host.")
            with suppress(OSError):
                Path(local_path).unlink()

        elif mode == 'rsync':
            self._sys(
                f"Using pre-uploaded file at {self._remote_path()}..."
            )
        else:
            raise ValueError(f"Unknown restore mode: {mode!r}")

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        remote = self._remote_path()
        dbname = inst.postgres_dbname or 'prod'

        return [
            (
                "Verify backup file",
                self.run_script("restore.sh", ["verify-file", d, remote]),
                {"stop_on_failure": True},
            ),
            (
                "Stop Odoo service",
                self.run_script("compose_op.sh", [d, "stop", "odoo"]),
                {"stop_on_failure": True},
            ),
            (
                "Restore database",
                self.run_script("restore.sh", ["restore-db", d, dbname, remote]),
                {"stop_on_failure": True},
            ),
            (
                "Ensure incubacloud_connect",
                self.run_script("restore.sh", ["ensure-connect", d, dbname]),
            ),
            (
                "Start Odoo service",
                self.run_script("compose_op.sh", [d, "start", "odoo"]),
            ),
            # Left inline: a lone ``rm -f`` is not worth a versioned
            # script, and unquoted the remote shell expands any ``~``.
            (
                "Remove remote backup file",
                f"rm -f {remote}",
            ),
        ]

    def parse_results(self, results):
        errors = []
        for label, data in results.items():
            if label in ("Remove remote backup file",
                         "Ensure incubacloud_connect"):
                continue
            if data.get('exit_status', 1) != 0:
                errors.append(
                    f"'{label}' exited with status"
                    f" {data.get('exit_status')}"
                )
        return errors

    async def on_success(self, results):
        self._sys("✓ Database restored successfully.")
        inst = self._inst()
        inst.write({"status": "ok", "running": True})
        if inst.pr_number:
            url = (
                f'https://{inst.domain}' if inst.domain
                else '_(no domain configured)_'
            )
            body = (
                f'✅ **IncubaCloud Preview** — ready!\n\n'
                f'| | |\n|---|---|\n'
                f'| **URL** | {url} |\n'
                f'| **Branch** | `{inst.pr_head_branch}` |\n'
                f'| **Instance** | `{inst.name}` |\n\n'
                f'_Auto-destroys when the PR is closed._'
            )
            inst._post_or_update_pr_comment(body)

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._inst().write({"status": "error"})
