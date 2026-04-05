from .abstract_executor import AbstractSSHExecutor


class StartInstanceExecutor(AbstractSSHExecutor):
    """Start a stopped doodba instance via docker compose up -d."""

    _job_type = "start_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        return [
            (
                "Start containers",
                f"cd {d} && docker compose up -d",
            ),
        ]

    async def on_success(self, results):
        inst = self._inst()
        self._sys(f"✓ Instance '{inst.name}' started.")
        inst.write({"running": True, "status": "ok"})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
