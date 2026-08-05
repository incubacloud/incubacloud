import os
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from .abstract_executor import AbstractSSHExecutor, validate_dup_time


class BackupDownloadNeutralizedExecutor(AbstractSSHExecutor):
    """Download a neutralized copy of a backup.

    A neutralized dump is safe for testing / troubleshooting: crons are
    disabled, mail servers archived, payment / OAuth / IAP credentials
    scrubbed, webhooks invalidated and the DB shows the red "Database
    neutralized for testing" banner. The process uses ``odoo neutralize``
    via ``click-odoo-restoredb --neutralize`` on a throwaway DB so the
    source instance is never modified.

    Works for both production (source: duplicity S3 backup at ``time``)
    and non-production (source: live DB dumped on demand when
    ``time='live'``).

    Payload:
        time: duplicity timestamp (e.g. '2026-03-19T02:00:00'),
              'latest' for the most recent S3 backup, or
              'live' to dump the current non-prod DB on the fly
        with_filestore: include filestore/ in the final ZIP (bool)
    """

    _job_type = "backup_download_neutralized"

    def _inst(self):
        return self.job.instance_id

    def _tmp_neutral_db(self):
        return f"__ic_neutral_{self.job.id}"

    def _host_tmp_dir(self):
        return f"/tmp/.incubacloud-bkneu-{self.job.id}"

    def _host_tmp_archive(self):
        return f"/tmp/.incubacloud-bkneu-{self.job.id}.zip"

    # ── hooks ────────────────────────────────────────────────────────────

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError(
                "backup_download_neutralized job has no instance_id"
            )
        if not inst.deployed:
            raise ValueError("Instance is not deployed.")
        payload = self.job.payload or {}
        if not payload.get('time'):
            raise ValueError("Missing 'time' in job payload.")
        # The value flows into ``--time "<value>"`` and into
        # ``sh -c 'dup restore --time "<value>" ...'``. The regex
        # below rejects whitespace, quotes, dollars, backticks and
        # semicolons — the only things that could break either shell.
        validate_dup_time(payload['time'])

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        payload = self.job.payload or {}
        raw_time = payload['time']
        with_filestore = bool(payload.get('with_filestore'))
        source_dbname = inst.postgres_dbname or 'prod'
        neutral_db = self._tmp_neutral_db()
        host_tmp = self._host_tmp_dir()
        archive = self._host_tmp_archive()
        is_prod = inst.environment == 'production'

        # Step 1: produce a source dump on the host, landing at
        # {host_tmp}/src.zip (non-prod live, or prod packaged from S3).
        if is_prod:
            step_prepare_src = (
                "Restore source dump from S3",
                self.run_script(
                    "backup_neutralized.sh",
                    [
                        "prepare-src-prod", d, self.job.id,
                        source_dbname, raw_time, host_tmp,
                    ],
                ),
                {"stop_on_failure": True},
            )
        else:
            # Non-prod: dump the live DB inside the odoo container.
            step_prepare_src = (
                "Dump live database",
                self.run_script(
                    "backup_neutralized.sh",
                    ["prepare-src-live", d, self.job.id,
                     source_dbname, host_tmp],
                ),
                {"stop_on_failure": True},
            )

        # Step 2: restore into a throwaway DB with --neutralize.
        # click-odoo-restoredb runs `odoo neutralize` which executes every
        # installed module's data/neutralize.sql (crons off, mail servers
        # archived, payment/OAuth/IAP credentials scrubbed, webhooks
        # invalidated, database.is_neutralized=true → red banner).
        step_restore_neutralize = (
            "Restore and neutralize in temp DB",
            self.run_script(
                "backup_neutralized.sh",
                ["restore-neutralize", d, self.job.id, neutral_db],
            ),
            {"stop_on_failure": True},
        )

        # Step 3: re-dump the neutralized DB.
        if with_filestore:
            step_redump = (
                "Create neutralized backup (DB + filestore)",
                self.run_script(
                    "backup_neutralized.sh",
                    ["redump-full", d, self.job.id, neutral_db, archive],
                ),
                {"stop_on_failure": True},
            )
        else:
            step_redump = (
                "Create neutralized backup (SQL only)",
                self.run_script(
                    "backup_neutralized.sh",
                    ["redump-sql", d, self.job.id, neutral_db,
                     host_tmp, archive],
                ),
                {"stop_on_failure": True},
            )

        # Step 4: drop the throwaway DB + cleanup source files.
        # Runs unconditionally via parse_results (non-stop_on_failure)
        # so cleanup still happens on the happy path.
        step_cleanup = (
            "Drop temp DB and cleanup",
            self.run_script(
                "backup_neutralized.sh",
                ["cleanup", d, self.job.id, neutral_db, host_tmp],
            ),
        )

        return [
            step_prepare_src,
            step_restore_neutralize,
            step_redump,
            step_cleanup,
        ]

    def parse_results(self, results):
        # Cleanup step is best-effort — do not fail the job if it errors.
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if label != "Drop temp DB and cleanup"
            and data.get('exit_status', 1) != 0
        ]

    async def after_commands(self, transport, results):
        await self._download_zip(transport)

    async def _download_zip(self, transport):
        inst = self._inst()
        archive = self._host_tmp_archive()
        payload = self.job.payload or {}
        raw = payload.get('time', 'live')
        time_label = (
            datetime.now().strftime('%Y%m%dT%H%M%S')
            if raw in ('latest', 'live') else raw
        )
        suffix = 'full' if payload.get('with_filestore') else 'dump'
        filename = f"{inst.name}-neutralized-{time_label}-{suffix}.zip"

        self._sys("Downloading neutralized backup to Odoo server…")
        local_fd, local_tmp = tempfile.mkstemp(
            prefix=".incubacloud-bkneu-", suffix=".zip",
        )
        os.close(local_fd)
        try:
            await transport.download_file(archive, local_tmp)

            self._sys("✓ Downloaded. Storing as attachment…")
            data = Path(local_tmp).read_bytes()

            with self.job.env.registry.cursor() as cr:
                env = self.job.env(cr=cr)
                # ``raw`` skips the in-RAM base64 copy of the archive.
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

        # Remote cleanup of the final archive.
        await transport.run(f"rm -f {archive}")

    async def on_success(self, results):
        self._sys(
            "✓ Neutralized backup ready for download (expires in 2 hours)."
        )

    async def on_failure(self, results, errors):
        # Best-effort cleanup so a failed run does not leave an orphan
        # __ic_neutral_<job_id> DB or stale temp files on the host.
        inst = self._inst()
        d = self._inst_dir(inst)
        neutral_db = self._tmp_neutral_db()
        host_tmp = self._host_tmp_dir()
        archive = self._host_tmp_archive()
        # Don't mask the original failure with a cleanup error.
        with suppress(Exception):
            async with self._host_record.get_transport() as transport:
                await transport.run(
                    f"cd {d} && docker compose exec --user root -T odoo"
                    f" sh -c 'dropdb --if-exists {neutral_db}"
                    f" && rm -rf /var/lib/odoo/filestore/{neutral_db}"
                    f" && rm -f /tmp/bkneu-src-{self.job.id}.*"
                    f" /tmp/bkneu-out-{self.job.id}.*'"
                    f" ; rm -rf {host_tmp} {archive}"
                )
        for err in errors:
            self._sys(f"✗ {err}")
