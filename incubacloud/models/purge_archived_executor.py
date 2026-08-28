"""Delete an archived instance's backup chain, then its record.

An archived instance is a record whose only remaining substance is the
chain in the bucket. Deleting it therefore cannot be a plain ``unlink``:
the invariant this whole feature rests on is that objects never outlive
the instance they belong to, and an unlink would leave the chain behind
with nothing left that even knows where it is.

There is no stack to purge from — archiving tore it down — so the work
runs in a container created for this one command and thrown away. See
``scripts/backup_purge_archived.sh`` for why that beats reviving the
instance in order to delete it.

The record is unlinked in ``on_success`` and nowhere else. A purge that
fails leaves the archived instance exactly as it was, which is the only
outcome that keeps the invariant true: better a record that refuses to
go than a bucket nobody can reach any more.
"""
import logging

from .abstract_executor import AbstractSSHExecutor

_logger = logging.getLogger(__name__)

#: Label of the single step, looked up by the failure classifier.
PURGE_ARCHIVED_LABEL = "Purge archived backups"

#: ``scripts/backup_purge_archived.sh`` exit codes mapped to the alert
#: each one raises, same contract as the delete executor's: the script
#: classifies from its own vantage point and this side never parses text.
PURGE_ARCHIVED_ALERT_BY_EXIT = {
    20: (
        "archived_purge_env_missing",
        "The credentials for %(name)s's archived copy never reached the "
        "host, so nothing could be deleted. Check that its backup "
        "destination still exists in the panel.",
    ),
    21: (
        "archived_purge_unauthorized",
        "The storage rejected the credentials while deleting %(name)s's "
        "archived copy. Check or regenerate them on the backup "
        "destination, then delete the instance again.",
    ),
    22: (
        "archived_purge_failed",
        "Could not delete %(name)s's archived copy. See the job log; the "
        "purge image failing to pull or to start is the usual cause.",
    ),
}

#: The prefix was already empty — the chain is gone by other means. Not
#: a failure: the invariant already holds, so the record may go.
PURGE_ARCHIVED_EXIT_ALREADY_EMPTY = 10


class PurgeArchivedBackupsExecutor(AbstractSSHExecutor):
    """Empty an archived instance's prefix, then remove its record."""

    _job_type = "purge_archived_backups"

    def _inst(self):
        # ``active_test=False``: the whole point is that this instance is
        # archived, so every default read would come back empty.
        return self.job.with_context(active_test=False).instance_id

    def _env_dir(self):
        """Per-job directory that holds the environment file."""
        return f"/tmp/.incubacloud-archived-purge-{self.job.id}"

    def _env_path(self):
        """Remote path of the uploaded environment file."""
        return f"{self._env_dir()}/env"

    def _env_content(self):
        """Render the environment the ephemeral container needs.

        Deliberately the same variable names the ``backup`` service was
        deployed with: the destination is read from ``DST`` and the
        credentials from the standard AWS variables, so the purge sees
        exactly what duplicity saw when it wrote the chain.

        ``custom_backup_dst`` and not the computed path — the frozen
        value is the only one still true once the project can have moved
        underneath the record.

        ``PURGE_BEFORE`` carries the instant the deletion was decided,
        so the purge is bounded to the chain that existed then. Without
        it a job that lands late — a retry, a queue that drained slowly —
        would empty whatever holds the prefix by the time it runs, and
        after "start from scratch" that is the customer's new instance.
        """
        inst = self._inst()
        backend = inst.effective_backup_backend.sudo()
        lines = [f"DST={inst.custom_backup_dst}"]
        if inst.purge_cutoff_at:
            lines.append(f"PURGE_BEFORE={inst.purge_cutoff_at.isoformat()}")
        if backend.s3_access_key_id:
            lines.append(f"AWS_ACCESS_KEY_ID={backend.s3_access_key_id}")
        if backend.s3_secret_access_key:
            lines.append(
                f"AWS_SECRET_ACCESS_KEY={backend.s3_secret_access_key}"
            )
        if backend.s3_endpoint_url:
            lines.append(f"AWS_ENDPOINT_URL={backend.s3_endpoint_url}")
        return "\n".join(lines) + "\n"

    async def before_execute(self, transport):
        """Create a private directory, then put the environment in it.

        Uploaded rather than passed as arguments: an argument is visible
        in ``ps`` to every account on the host for as long as the
        container runs, and this one carries the destination's
        credentials.

        The directory is made 0700 *before* the file exists, because
        SFTP writes it with the default mask and there is no moment when
        a world-readable copy should be reachable — not even the
        milliseconds a later ``chmod`` would leave open. The script
        removes both on its way out, failure included.
        """
        await transport.execute(
            f"install -d -m 700 {self._env_dir()}",
            self.on_stdout, self.on_stderr,
        )
        await transport.upload_text_files(
            {self._env_path(): self._env_content()},
        )

    def get_commands(self):
        image = (
            self.env["cloud.settings"].sudo()._get_system().archived_purge_image
        )
        return [(
            PURGE_ARCHIVED_LABEL,
            self.run_script(
                "backup_purge_archived.sh", [self._env_path(), image],
            ),
            {"stop_on_failure": True},
        )]

    def parse_results(self, results):
        """Treat "already empty" as success, everything else as reported.

        Exit 10 means the prefix holds nothing. The record is free to go:
        what the deletion had to guarantee is already true.
        """
        step = results.get(PURGE_ARCHIVED_LABEL) or {}
        if step.get("exit_status") == PURGE_ARCHIVED_EXIT_ALREADY_EMPTY:
            return []
        return super().parse_results(results)

    async def on_success(self, results):
        """Drop the record now that nothing of it is left in the bucket.

        The order is the point: the objects go first and the record only
        afterwards, so an interruption anywhere in between leaves a
        record whose chain is empty — recoverable by deleting it again —
        rather than a chain with no record, which nothing could ever
        find.
        """
        inst = self._inst()
        name = inst.name if inst else "?"
        self._sys(f"✓ Archived copy of '{name}' deleted.")
        if not inst:
            return
        Alert = self.env["cloud.alert"].sudo()
        for code, _msg in PURGE_ARCHIVED_ALERT_BY_EXIT.values():
            Alert.resolve_alert(code, instance=inst)
        Alert.resolve_alert("archive_copy_lost", instance=inst)
        inst.sudo().unlink()
        _logger.info("archived instance %s deleted with its chain", name)
        # Whatever the instance leaves behind — an emptied project, a
        # tenant link — is cleaned up by ``unlink`` itself, layer by
        # layer. Doing it from here would mean core deciding to delete a
        # project because its last instance went, which is true of a
        # tenant's disposable project and wrong for an operator's.
        await self._after_purge()

    async def _after_purge(self):
        """Hook: the copy is gone and the record with it.

        Empty in core, where deleting an archived instance is the end of
        the story. A module that owns a lifecycle *above* the instance —
        a tenant, say — may need to act exactly here and nowhere
        earlier: a customer who asks to start over must not have a fresh
        instance provisioned until this has run, because it would take
        the same name and therefore the same backup prefix, and this
        purge would empty the new instance's own copy.

        Called after the unlink, so an override that needs anything off
        the instance has to read it before, in its own ``on_success``.
        """

    async def on_failure(self, results, errors):
        """Report, alert, and leave the record standing."""
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if not inst:
            return
        exit_status = (
            results.get(PURGE_ARCHIVED_LABEL) or {}
        ).get("exit_status")
        code_message = PURGE_ARCHIVED_ALERT_BY_EXIT.get(exit_status)
        if not code_message:
            code_message = PURGE_ARCHIVED_ALERT_BY_EXIT[22]
        code, template = code_message
        self.env["cloud.alert"].sudo().raise_alert(
            code,
            template % {"name": inst.name},
            level="warning",
            host=self._host(),
            instance=inst,
            job=self.job,
        )
        self._sys(
            "The instance was NOT deleted: its archived copy is still in "
            "the storage, and removing the record now would leave it "
            "there with nothing able to find it. Fix the cause above and "
            "delete again."
        )
