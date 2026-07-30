from .abstract_executor import AbstractSSHExecutor


class RestartInstanceExecutor(AbstractSSHExecutor):
    """Restart a doodba instance via docker compose restart."""

    _job_type = "restart_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    def get_commands(self):
        inst = self._inst()
        return [
            (
                "Restart containers",
                self.run_script(
                    "compose_op.sh", [self._inst_dir(inst), "restart"],
                ),
            ),
        ]

    async def on_success(self, results):
        inst = self._inst()
        self._sys(f"✓ Instance '{inst.name}' restarted.")
        inst.write({"running": True, "status": "ok"})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
