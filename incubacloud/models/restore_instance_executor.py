import os
import tempfile
from contextlib import suppress
from pathlib import Path

from ..restore_staging import is_staged_upload
from .abstract_executor import (
    AbstractSSHExecutor,
    handoff_archive_path,
    sql_escape_literal,
)


class RestoreInstanceExecutor(AbstractSSHExecutor):
    """Restore a doodba instance database from a .zip backup.

    Payload:
        mode: 'browser' | 'from_job' | 'from_host' | 'rsync' — where the
            zip comes from. ``from_host`` consumes the archive a chained
            ``backup_download`` with ``handoff='host'`` left on its host:
            same host → the archive is restored (and removed) in place,
            nothing crosses the wire; different host → it is streamed
            source host → core disk → this host, never through RAM or an
            ``ir.attachment``. ``from_job`` (the attachment relay) stays
            for operator-facing flows and old queued jobs.
        local_path / source_job_id: source, per mode
        neutralize: run ``click-odoo-restoredb --neutralize`` so the
            restored copy comes up with scheduled actions and outgoing
            mail disabled and the test banner on. Callers that copy a
            production database into another instance (clone to staging,
            refresh from production) set it; a host move must not, since
            that is the same production instance changing machines.
        reset_base_url: overwrite the ``web.base.url`` that travelled
            inside the dump with this instance's own domain, and drop
            ``web.base.url.freeze``. Skipped when the instance has no
            domain — writing an empty parameter would be worse than
            leaving the inherited one.
    """

    _job_type = "restore_instance"

    def _inst(self):
        return self.job.instance_id

    def _source_job(self):
        """Return the chained job referenced by ``source_job_id``, or an
        empty recordset when the payload carries none."""
        source_job_id = (self.job.payload or {}).get('source_job_id')
        if not source_job_id:
            return self.job.browse()
        return self.job.browse(int(source_job_id)).exists()

    def _remote_path(self):
        """Path of the archive on this job's host.

        ``from_host`` on the producer's own host restores the handoff
        archive in place (the trailing cleanup step then removes it);
        every other mode stages the zip at an instance-keyed path first.
        Computed from the payload alone so it needs no ordering with
        ``before_execute``.
        """
        payload = self.job.payload or {}
        if payload.get('mode') == 'from_host':
            source = self._source_job()
            if source and source.host_id == self.job.host_id:
                return handoff_archive_path(source.id)
        if payload.get('mode') == 'ssh_upload':
            grant = self._grant()
            if grant:
                # Inside the grant's own directory: that is the only
                # place its key could have written, so reading anywhere
                # else would mean reading something it did not put there.
                return grant._remote_path(
                    grant.received_filename or 'restore.zip',
                )
        return f"/tmp/incubacloud-restore-{self._inst().id}.zip"

    def _download_command(self, remote):
        """Return the step that fetches the archive onto the host.

        Three deliberate choices. ``-C -`` resumes, because this exists
        for archives too big to send twice. Redirects are *not* followed:
        the address was validated and pinned here, and a redirect is
        precisely how that validation would be escaped. And the
        credentials, if any, go in a netrc file created and removed
        inside the same step — a password in the command line would be
        readable in ``ps`` by anyone on the host.

        :param remote: path the archive must land on.
        :return: an executor command tuple.
        """
        payload = self.job.payload or {}
        url = payload.get('url') or ''
        pin = payload.get('resolve') or ''
        netrc = f"{remote}.netrc"
        credential = self.job._restore_url_credential()
        pin_arg = f" --resolve {pin}" if pin else ""
        if credential:
            machine, user, password = credential
            write_netrc = (
                f"umask 077; printf '%s\\n'"
                f" 'machine {machine} login {user} password {password}'"
                f" > {netrc}; "
            )
            netrc_arg = f" --netrc-file {netrc}"
            cleanup = f"; rc=$?; rm -f {netrc}; exit $rc"
        else:
            write_netrc = ""
            netrc_arg = ""
            cleanup = ""
        return (
            "Download the archive",
            f"{write_netrc}curl -fsS -C - --max-time 86400"
            f"{pin_arg}{netrc_arg} -o {remote} '{url}'{cleanup}",
            {"stop_on_failure": True},
        )

    def _grant(self):
        """Return the upload grant this job consumes, if it names one."""
        grant_id = (self.job.payload or {}).get('grant_id')
        if not grant_id:
            return self.env['cloud.restore.upload.grant'].browse()
        return self.env['cloud.restore.upload.grant'].browse(
            int(grant_id),
        ).exists()

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
            # Defense-in-depth: ``local_path`` must name a file the
            # /cloud/instance/<id>/restore controller staged for *this*
            # instance. Anything else (e.g. ``/etc/odoo/odoo.conf`` passed
            # directly via JSON-RPC) is rejected — the executor would
            # otherwise both upload and unlink the target file.
            if not is_staged_upload(local_path, self._inst().id):
                raise ValueError(
                    "local_path must be an upload staged by the restore "
                    "controller for this instance (got %r)." % (local_path,)
                )
            resolved = str(Path(local_path).resolve())
            if not Path(resolved).exists():
                raise ValueError(
                    "Backup file not found on Odoo server. "
                    "Please re-upload and try again."
                )
            # ``finally``: the archive is up to 2 GiB and this is the only
            # code that deletes it. Leaving it behind on a failed transfer
            # (host down, disk full, connection dropped) would pin that
            # much disk until the container is recreated, and the operator
            # retries by uploading again anyway.
            try:
                self._sys("Uploading backup to remote host via SFTP...")
                await transport.upload_file(resolved, self._remote_path())
                self._sys("✓ Backup transferred to remote host.")
            finally:
                with suppress(OSError):
                    Path(resolved).unlink()

        elif mode == 'from_job':
            source_job_id = payload.get('source_job_id')
            if not source_job_id:
                raise ValueError("Missing source_job_id in payload")
            att = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'cloud.job'),
                ('res_id', '=', int(source_job_id)),
            ], limit=1, order='id desc')
            # ``raw`` reads the filestore bytes directly; ``datas`` would
            # materialise a second, base64 copy of the archive in RAM.
            data = att.raw if att else b""
            if not data:
                raise ValueError(
                    "Backup attachment not found from source job"
                )
            self._sys("Retrieving backup from previous job…")
            fd, local_path = tempfile.mkstemp(
                prefix=".incubacloud-restore-", suffix=".zip",
            )
            os.close(fd)
            try:
                Path(local_path).write_bytes(data)
                self._sys("Uploading backup to remote host via SFTP…")
                await transport.upload_file(local_path, self._remote_path())
                self._sys("✓ Backup transferred to remote host.")
            finally:
                with suppress(OSError):
                    Path(local_path).unlink()

        elif mode == 'from_host':
            source = self._source_job()
            if not source:
                raise ValueError(
                    "Missing or unknown source_job_id in payload"
                )
            if not source.host_id:
                raise ValueError("Source job has no host")
            src_path = handoff_archive_path(source.id)
            if source.host_id == self.job.host_id:
                # Producer and consumer share the host: the archive is
                # already where _remote_path() points. The "Verify backup
                # file" step confirms it exists.
                self._sys(f"Using backup staged on this host ({src_path}).")
                return
            # Cross-host: relay through the core's *disk* — both SFTP
            # legs stream file-to-file, so memory stays flat no matter
            # the archive size. The source copy is only removed in
            # on_success, which keeps a failed restore retryable.
            self._sys(
                f"Fetching backup from {source.host_id.name}…"
            )
            fd, local_path = tempfile.mkstemp(
                prefix=".incubacloud-handoff-", suffix=".zip",
            )
            os.close(fd)
            try:
                async with source.host_id.get_transport() as src_transport:
                    await src_transport.download_file(src_path, local_path)
                self._sys("Uploading backup to this host via SFTP…")
                await transport.upload_file(local_path, self._remote_path())
            finally:
                with suppress(OSError):
                    Path(local_path).unlink()
            self._sys("✓ Backup transferred to this host.")

        elif mode == 'rsync':
            self._sys(
                f"Using pre-uploaded file at {self._remote_path()}..."
            )

        elif mode == 'ssh_upload':
            grant = self._grant()
            if not grant:
                raise ValueError(
                    "The upload grant for this restore no longer exists."
                )
            self._sys(
                f"Using the archive uploaded through the temporary key "
                f"({grant.received_filename or 'restore.zip'})…"
            )
            # The key has done its job. Closing it here rather than at the
            # end means a restore that fails still leaves no door open —
            # the archive is already on the host, so a retry needs no
            # second upload.
            grant._mark_used()
            self.env['cloud.job'].enqueue(
                grant.host_id.id, grant.instance_id.id,
                'revoke_restore_upload_key',
                payload={'grant_id': grant.id, 'keep_directory': True},
                bypass_running_check=True,
            )

        elif mode == 'from_url':
            self._sys("Downloading the archive on the host…")

        else:
            raise ValueError(f"Unknown restore mode: {mode!r}")

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        remote = self._remote_path()
        dbname = inst.postgres_dbname or 'prod'
        payload = self.job.payload or {}
        neutralize = "1" if payload.get('neutralize') else "0"

        cmds = []
        if payload.get('mode') == 'from_url':
            cmds.append(self._download_command(remote))
        cmds += [
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
                self.run_script(
                    "restore.sh",
                    ["restore-db", d, dbname, remote, neutralize],
                ),
                {"stop_on_failure": True},
            ),
        ]

        # Before "Ensure incubacloud_connect" on purpose: that step boots
        # an Odoo which reads web.base.url. ``db`` stays up throughout the
        # restore (only ``odoo`` is stopped), so psql over compose works.
        base_url = self._base_url() if payload.get('reset_base_url') else ""
        if base_url:
            cmds.append((
                "Reset base URL",
                self.run_script(
                    "restore.sh",
                    [
                        "set-base-url", d,
                        inst.postgres_username or "odoo",
                        dbname,
                        sql_escape_literal(base_url),
                        "http://localhost:8069",
                    ],
                ),
            ))

        cmds += [
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
        return cmds

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
        await self._cleanup_cross_host_handoff()
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

    async def _cleanup_cross_host_handoff(self):
        """Best-effort removal of the handoff archive on the *source*
        host after a successful cross-host restore.

        Same-host needs nothing here: ``_remote_path()`` is the handoff
        path itself and the trailing "Remove remote backup file" step
        already deleted it. On failure the archive is left in place on
        purpose — a retried restore fetches it again instead of dying on
        a missing file (/tmp clears it on the next host reboot at the
        latest).
        """
        payload = self.job.payload or {}
        if payload.get('mode') != 'from_host':
            return
        source = self._source_job()
        if not source or not source.host_id \
                or source.host_id == self.job.host_id:
            return
        with suppress(Exception):
            async with source.host_id.get_transport() as src_transport:
                await src_transport.run(
                    f"rm -f {handoff_archive_path(source.id)}"
                )
            self._sys("✓ Staged backup removed from the source host.")

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._inst().write({"status": "error"})
