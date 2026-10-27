from .abstract_executor import COMPOSE_OP_EXIT_UNCHECKED, AbstractSSHExecutor


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
                {"continue_on": (COMPOSE_OP_EXIT_UNCHECKED,)},
            ),
        ]

    def parse_results(self, results):
        """Fail-closed like the base, except when only the check failed.

        ``compose_op.sh`` exits ``COMPOSE_OP_EXIT_UNCHECKED`` when the
        containers restarted but the steps that follow could not be
        checked: the job succeeds, with a warning in its log.
        """
        return super().parse_results(
            self._accept_unchecked_compose_op(results, "Restart containers"),
        )

    async def on_success(self, results):
        inst = self._inst()
        self._sys(f"✓ Instance '{inst.name}' restarted.")
        inst.write({**inst._running_confirmed_vals(), "status": "ok"})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
