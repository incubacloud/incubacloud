from .abstract_executor import AbstractSSHExecutor


class DeleteInstanceExecutor(AbstractSSHExecutor):
    """Stop and remove a doodba instance from the remote host."""

    _job_type = "delete_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        return [
            # 1. Shut down containers (skip gracefully if dir is absent)
            (
                "Stop and remove containers",
                f"if [ -d {d} ]; then"
                f"  cd {d} && docker compose down --rmi all -v --remove-orphans;"
                f"else"
                f"  echo 'Directory {d} not found, nothing to shut down.';"
                f"fi",
            ),
            # 2. Remove the instance directory (idempotent)
            (
                "Remove instance directory",
                f"rm -rf {d}",
            ),
        ]

    async def on_success(self, results):
        inst = self._inst()
        name = inst.name if inst else "?"
        self._sys(f"✓ Instance '{name}' removed from host.")
        if inst:
            inst.write({'active': False})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
