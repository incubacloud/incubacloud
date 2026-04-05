from .abstract_executor import AbstractSSHExecutor


class StopInstanceExecutor(AbstractSSHExecutor):
    """Stop a running doodba instance via docker compose stop."""

    _job_type = "stop_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        return [
            (
                "Stop containers",
                f"cd {d} && docker compose stop",
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
