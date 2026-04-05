import base64
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from .abstract_executor import AbstractSSHExecutor

_TMP_PREFIX = "/tmp/.incubacloud"


class ExportInstanceExecutor(AbstractSSHExecutor):
    """Package the instance folder as a downloadable tarball.

    Uses ``tar --exclude-vcs-ignores`` to respect the instance's
    ``.gitignore``, automatically skipping cloned VCS repos (odoo,
    enterprise, queue…), build artefacts and other ignored paths —
    mirroring what a ``git archive`` would produce.  Explicit excludes
    cover common doodba data dirs (dumps, postgres, secrets).

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
        return f"{_TMP_PREFIX}-export-{self._inst().name}.tar.gz"

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("export_instance job has no instance_id")

        d = self._inst_dir(inst)
        remote_tmp = self._remote_tmp()
        self._sys(f"Creating export archive for '{inst.name}'…")

        # Strategy: copy the project to a temp dir, sanitize secrets,
        # then tar the clean copy.
        staging = f"{_TMP_PREFIX}-export-staging-{inst.name}"
        sanitize_script = (
            f"set -euo pipefail && "
            # 1. Copy project (respecting .gitignore via rsync --filter)
            f"rm -rf {staging} && mkdir -p {staging} && "
            f"cd {d} && "
            f"rsync -a "
            f"  --exclude='.git' "
            f"  --exclude='odoo/auto' "
            f"  --exclude='dumps' "
            f"  --exclude='postgres' "
            f"  --exclude='secrets' "
            f"  . {staging}/ && "
            # 2. Strip tokens from repos.yaml URLs
            f"if [ -f {staging}/odoo/custom/src/repos.yaml ]; then "
            f"  sed -i 's|://x-access-token:[^@]*@|://|g' "
            f"    {staging}/odoo/custom/src/repos.yaml; "
            f"fi && "
            # 3. Remove SSH keys
            f"rm -f {staging}/odoo/custom/ssh/id_rsa "
            f"  {staging}/odoo/custom/ssh/id_rsa.pub "
            f"  {staging}/odoo/custom/ssh/known_hosts && "
            # 4. Sanitize .docker env files — replace secrets with placeholders
            f"if [ -d {staging}/.docker ]; then "
            f"  echo 'ADMIN_PASSWORD=changeme' > {staging}/.docker/odoo.env; "
            f"  echo 'PGPASSWORD=changeme' > {staging}/.docker/db-access.env; "
            f"  echo 'POSTGRES_PASSWORD=changeme' > {staging}/.docker/db-creation.env; "
            f"  rm -f {staging}/.docker/backup.env; "
            f"fi && "
            # 5. Remove backup_dst from copier answers
            f"if [ -f {staging}/.copier-answers.yml ]; then "
            f"  sed -i '/^backup_dst:/d' {staging}/.copier-answers.yml; "
            f"fi && "
            # 6. Create tarball from sanitized copy
            f"cd {staging} && "
            f"tar -czf {remote_tmp} . && "
            f"rm -rf {staging} && "
            f"echo \"SIZE:$(du -sh {remote_tmp} | cut -f1)\""
        )

        try:
            result = await transport.run(sanitize_script)
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
                with self.job.env.registry.cursor() as cr:
                    env = self.job.env(cr=cr)
                    attachment = env["ir.attachment"].create({
                        "name": filename,
                        "type": "binary",
                        "datas": base64.b64encode(data).decode("ascii"),
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
