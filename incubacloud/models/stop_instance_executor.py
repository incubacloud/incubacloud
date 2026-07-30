from .abstract_executor import AbstractSSHExecutor


class StopInstanceExecutor(AbstractSSHExecutor):
    """Stop a running doodba instance via docker compose stop."""

    _job_type = "stop_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    def get_commands(self):
        """Return the command(s) to stop a running doodba instance.

        By default stops all compose services. When ``payload.services``
        is a non-empty list of service names, only those services are
        stopped — used by ``move_to_host`` to quiesce ``odoo`` while
        leaving ``db`` and ``backup`` alive for the subsequent
        ``backup_create``/``backup_download`` steps.
        """
        inst = self._inst()
        payload = self.job.payload or {}
        services = [s for s in (payload.get('services') or []) if s]
        return [
            (
                "Stop containers",
                self.run_script(
                    "compose_op.sh",
                    [self._inst_dir(inst), "stop", *services],
                ),
            ),
        ]

    async def on_success(self, results):
        inst = self._inst()
        self._sys(f"✓ Instance '{inst.name}' stopped.")
        inst.write({"running": False})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
