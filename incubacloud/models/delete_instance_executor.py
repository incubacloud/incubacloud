from .abstract_executor import AbstractSSHExecutor


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

    async def on_success(self, results):
        inst = self._inst()
        name = inst.name if inst else "?"
        self._sys(f"✓ Instance '{name}' removed from host.")
        # Read the host before _finalize_removal, which may unlink the
        # instance and take the link with it.
        host = inst.host_id if inst else self._host()
        if inst:
            keep_in_panel = bool((self.job.payload or {}).get("keep_in_panel"))
            inst._finalize_removal(keep_in_panel)
        if host:
            host.refresh_observability_labels(reason="instance removed")

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
            # The teardown failed, so the instance is still on the host:
            # back to 'deployed' rather than stuck in 'deleting'.
            if inst.state == "deleting":
                inst._transition("deployed")
