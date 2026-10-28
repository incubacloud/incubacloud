import os
import tempfile
from contextlib import suppress
from pathlib import Path

from ._odoo_versions import script_env
from .abstract_executor import AbstractSSHExecutor


class ExportInstanceExecutor(AbstractSSHExecutor):
    """Package the instance folder as a downloadable development copy.

    ``scripts/export_sanitize.sh`` copies the project with ``rsync`` and
    explicit excludes: VCS checkouts, data directories (dumps, postgres,
    secrets), every env file, the logs, the override with the
    platform's resource limits and the continuous recovery agent's
    ``.walg/``. It then strips repository tokens, SSH
    keys and credentials from the Odoo conf and the copier answers,
    points ``docker-compose.yml`` at ``devel.yaml`` and adds a
    ``DEVELOPMENT.md`` with the steps to start it.

    The tarball is stored as an ``ir.attachment`` linked to the job.
    ``cloud.job._format()`` derives the download URL from that
    attachment, so we never write to ``cloud_job`` from inside the
    executor (which would cause a PostgreSQL SSI serialisation conflict
    with the queue_job main cursor).
    """

    _job_type = "export_instance"
    _attachment_id = None

    def _inst(self):
        return self.job.instance_id

    def _remote_tmp(self):
        """Return where the archive is built: the job's own directory.

        :rtype: str
        """
        return self._job_path("export.tar.gz")

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("export_instance job has no instance_id")

        d = self._inst_dir(inst)
        remote_tmp = self._remote_tmp()
        self._sys(f"Creating export archive for '{inst.name}'…")

        # Strategy: copy the project to a temp dir, sanitize secrets,
        # then tar the clean copy. The sanitizer runs here rather than
        # through ``get_commands`` because everything after it needs the
        # archive to already exist, so the scripts are uploaded by hand
        # (``_async_entry``'s own upload happens later and then no-ops).
        # The copy holds the project before its secrets are stripped: in
        # the job's own directory, which ``_upload_scripts`` creates.
        staging = self._job_path("export-staging")
        sanitize_command = self.run_script(
            "export_sanitize.sh", [d, staging, remote_tmp],
            env=script_env(inst.odoo_version),
        )
        await self._upload_scripts(transport)

        try:
            result = await transport.run(sanitize_command)
            if result.exit_status != 0:
                raise RuntimeError(
                    "Failed to create export archive: "
                    + (result.stdout or f"exit status {result.exit_status}")
                )

            size_label = ""
            for line in result.stdout.splitlines():
                if line.startswith("SIZE:"):
                    size_label = f" ({line[5:].strip()})"
                    break

            self._sys(
                f"✓ Archive created{size_label}."
                " Downloading to Odoo server…"
            )

            # Download via SFTP to a local temp file on the Odoo server.
            local_fd, local_tmp = tempfile.mkstemp(
                prefix=".incubacloud-export-", suffix=".tar.gz"
            )
            os.close(local_fd)
            try:
                await transport.download_file(remote_tmp, local_tmp)

                self._sys("✓ Downloaded. Storing as attachment…")

                data = Path(local_tmp).read_bytes()

                filename = f"{inst.name}-export.tar.gz"
                with self._durable_env() as env:
                    # ``raw`` skips the in-RAM base64 copy of the tarball.
                    attachment = env["ir.attachment"].create({
                        "name": filename,
                        "type": "binary",
                        "raw": data,
                        "res_model": "cloud.job",
                        "res_id": self.job.id,
                    })
                    self._attachment_id = attachment.id

                self._sys(
                    "✓ Ready. Open the Jobs panel and click Download."
                )
            finally:
                with suppress(OSError):
                    Path(local_tmp).unlink()

        except Exception:
            # Best-effort remote cleanup; ignore errors.
            await transport.run(f"rm -f {remote_tmp} && rm -rf {staging}")
            raise

    def get_commands(self):
        return [
            (
                "Clean up remote temp file",
                f"rm -f {self._remote_tmp()}",
            ),
        ]

    def parse_results(self, results):
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get("exit_status", 1) != 0
        ]

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
