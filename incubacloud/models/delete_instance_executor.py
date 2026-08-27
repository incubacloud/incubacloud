from .abstract_executor import AbstractSSHExecutor

#: Label of the purge step. The failure classifier looks the step up by
#: this exact string, so it lives here rather than being repeated.
PURGE_LABEL = "Purge backups"

#: Exit codes ``scripts/backup_purge.sh`` promises, mapped to the alert
#: each one raises. The script classifies; this side never parses text,
#: because duplicity's and boto's wording changes between versions and a
#: broken matcher would silently downgrade every diagnosis to "unknown".
PURGE_ALERT_BY_EXIT = {
    20: (
        "backup_purge_service_missing",
        "The panel expects a backup container for %(name)s but the host's "
        "compose does not declare one. Rebuild the instance, then delete "
        "it again.",
    ),
    21: (
        "backup_purge_unauthorized",
        "The storage rejected the credentials while clearing %(name)s's "
        "backups. Check or regenerate them on the backup destination, "
        "then delete the instance again.",
    ),
    22: (
        "backup_purge_failed",
        "Could not clear %(name)s's backups. See the job log for what the "
        "backup container reported; a container that fails to start is "
        "the usual cause.",
    ),
}

#: The prefix was already empty. Not a failure: the invariant this step
#: exists to uphold — no objects left behind by a vanished instance —
#: already holds, so the teardown proceeds.
PURGE_EXIT_ALREADY_EMPTY = 10


class DeleteInstanceExecutor(AbstractSSHExecutor):
    """Stop and remove a doodba instance from the remote host."""

    _job_type = "delete_instance"

    # Whether tearing down the remote copy also ends the instance's
    # lifecycle. The move cleanups reuse these teardown commands on a
    # host the instance no longer lives on, so they set this to False:
    # for them the record must stay untouched.
    _owns_instance_lifecycle = True

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    def _keeps_the_record(self):
        """Whether this run leaves the instance in the panel as a draft."""
        return bool((self.job.payload or {}).get("keep_in_panel"))

    def _purge_step(self, inst, inst_dir):
        """Return the backup-purge step, or nothing when there is none.

        Deleting an instance must not leave its backups behind: nothing
        would ever prune them (the retention job runs inside the very
        container this teardown destroys) and, on a managed destination,
        they keep consuming a paid quota nobody can reclaim from the
        panel. So the purge runs *first* and, if it fails,
        ``stop_on_failure`` aborts before the teardown — otherwise the
        objects would be stranded with nothing left able to reach them.

        Three gates, and each one guards a different mistake:

        ``_owns_instance_lifecycle`` — the move cleanups reuse these
        teardown commands on a host the instance no longer lives on (the
        abandoned source copy, or a rolled-back half-built target). The
        instance is alive elsewhere and its backups are *its own*:
        purging there would destroy the backups of a running instance.

        ``keep_in_panel`` — keeping the record is not a deletion, so
        those backups still have an instance they belong to.

        ``expected_services()`` — whether there is anything to purge is
        decided here, never from what the host answers. It is the single
        source of truth for which containers this instance renders, so
        the purge cannot ask for one that was never deployed. Note it is
        stricter than ``_backup_enabled()`` alone: a *staging* instance
        resolves a backup destination whenever a global default exists,
        yet only production ever renders the container — gating on the
        flag alone would send the purge after a container that is not
        there and make every staging instance impossible to delete.

        It is also what keeps Free tenants out without a single branch
        naming them: their plan disables the container, so it is absent
        here for the same reason it is absent on the host.
        """
        if not self._owns_instance_lifecycle:
            return ()
        if self._keeps_the_record():
            return ()
        if "backup" not in inst.expected_services():
            return ()
        return ((
            PURGE_LABEL,
            self.run_script("backup_purge.sh", [inst_dir]),
            {"stop_on_failure": True},
        ),)

    def _purge_exit_status(self, results):
        """Return the purge step's exit code, or None if it did not run."""
        data = (results or {}).get(PURGE_LABEL)
        return data.get("exit_status") if data else None

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def before_execute(self, transport):
        """Enter 'deleting' now that the host answered.

        Written on its own cursor so the state is visible to everything
        else (a second Delete, a deploy) while the teardown runs;
        ``on_failure`` puts it back to 'deployed' if the teardown fails.
        Skipped when the instance is already 'deleting', so re-running a
        job that died mid-teardown is not rejected by the transition map.
        """
        inst = self._inst()
        if not self._owns_instance_lifecycle or not inst:
            return
        with self.job.env.registry.cursor() as cr:
            fresh = self.job.env(cr=cr)["cloud.instance"].browse(inst.id)
            if fresh.exists() and fresh.state == "deployed":
                fresh._transition("deleting")

    def get_commands(self):
        inst = self._inst()
        if not inst.exists():
            # Nothing left to tear down: an earlier attempt already
            # finished and unlinked the record. This job is running
            # again because its own transaction lost a serialization
            # race against the sibling deletes committing at the same
            # instant and queue_job re-queued it — not because the
            # teardown failed. ``on_success`` commits on its own cursor,
            # so the remote work survived that rollback; re-running has
            # to be a no-op or a completed removal reports as failed and
            # raises an alert for a host that is already clean.
            #
            # Only the *unlinked* case lands here. An archived instance
            # still exists and still has its directory on the host, so
            # it goes down the normal path below.
            self._sys("Instance already removed from the host; nothing to do.")
            return []
        d = self._inst_dir(inst)
        return [
            *self._purge_step(inst, d),
            # 1. Shut down containers (the script skips a missing dir)
            (
                "Stop and remove containers",
                self.run_script("compose_op.sh", [d, "down"]),
            ),
            # 2. Drop the host's logrotate config for this instance.
            #    The logs themselves go with the directory below; a
            #    leftover config would point at a path that no longer
            #    exists and make logrotate complain nightly forever.
            (
                "Remove log rotation config",
                self.run_script("instance_logs.sh", [
                    "remove", inst.doodba_project_name,
                ]),
            ),
            # 3. Remove the instance directory (idempotent). Left inline
            #    on purpose: a lone ``rm -rf`` is not an operation worth
            #    a versioned script, and unquoted it lets the remote
            #    shell expand the ``~`` in the path.
            (
                "Remove instance directory",
                f"rm -rf {d}",
            ),
        ]

    def parse_results(self, results):
        """Fail-closed like the base, except for "already empty".

        The purge answers 10 when the prefix held nothing. That is not a
        failure to recover from: the invariant it defends — no objects
        left over from an instance that no longer exists — is already
        satisfied, and refusing to continue would make an instance whose
        bucket someone emptied by hand impossible to delete.
        """
        if self._purge_exit_status(results) == PURGE_EXIT_ALREADY_EMPTY:
            results = {
                label: data for label, data in results.items()
                if label != PURGE_LABEL
            }
        return super().parse_results(results)

    def _purge_alert_targets(self):
        """Alert codes this executor owns, for raising and resolving."""
        return [code for code, _msg in PURGE_ALERT_BY_EXIT.values()]

    async def on_success(self, results):
        inst = self._inst()
        name = inst.name if inst else "?"
        # A clean run closes whatever a previous attempt complained
        # about; leaving them lit would keep an operator chasing a
        # problem that is already gone.
        if inst:
            Alert = self.env["cloud.alert"].sudo()
            for code in self._purge_alert_targets():
                Alert.resolve_alert(code, instance=inst)
        self._sys(f"✓ Instance '{name}' removed from host.")
        # Read the host before _finalize_removal, which may unlink the
        # instance and take the link with it.
        host = inst.host_id if inst else self._host()
        if inst:
            inst._finalize_removal(self._keeps_the_record())
        if host:
            host.refresh_observability_labels(reason="instance removed")

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            self._alert_on_purge_failure(results, inst)
            inst.write({"status": "error"})
            # The teardown failed, so the instance is still on the host:
            # back to 'deployed' rather than stuck in 'deleting'.
            if inst.state == "deleting":
                inst._transition("deployed")

    def _alert_on_purge_failure(self, results, inst):
        """Raise the alert that names why the backups could not be cleared.

        A single "delete failed" would send the operator to the job log
        every time. Each code here has a different fix, so each gets its
        own message and its own dedup key.

        Classified from the script's exit code alone. Reading duplicity's
        or boto's wording would look more precise and age badly: the day
        either changes its phrasing every diagnosis silently becomes the
        catch-all, and nothing fails to announce it.
        """
        code_message = PURGE_ALERT_BY_EXIT.get(
            self._purge_exit_status(results)
        )
        if not code_message:
            # The purge is not what failed — the teardown itself did.
            return
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
            "The instance was NOT deleted: its backups are still in the "
            "storage, and removing it now would strand them there with "
            "nothing able to reach them. Fix the cause above and delete "
            "again."
        )
