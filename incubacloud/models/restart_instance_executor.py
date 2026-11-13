from .abstract_executor import (
    COMPOSE_OP_EXIT_PREVIOUS_BUILD,
    COMPOSE_OP_EXIT_UNAPPLIED,
    COMPOSE_OP_EXIT_UNCHECKED,
    AbstractSSHExecutor,
)


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
                {"continue_on": (
                    COMPOSE_OP_EXIT_UNCHECKED, COMPOSE_OP_EXIT_PREVIOUS_BUILD,
                )},
            ),
        ]

    def parse_results(self, results):
        """Fail-closed like the base, except when only the check failed.

        ``compose_op.sh`` exits ``COMPOSE_OP_EXIT_UNCHECKED`` when the
        containers restarted but the steps that follow could not be
        checked, and ``COMPOSE_OP_EXIT_PREVIOUS_BUILD`` when a rebuild
        left its files and image unapplied and the containers the instance
        had were restarted instead of recreated: the job succeeds, with a
        warning in its log. ``COMPOSE_OP_EXIT_UNAPPLIED`` fails it saying
        why and what to do (#96).
        """
        refusal = self._unapplied_refusal(results, "Restart containers")
        if refusal:
            return [refusal]
        results = self._accept_previous_build(results, "Restart containers")
        return super().parse_results(
            self._accept_unchecked_compose_op(results, "Restart containers"),
        )

    async def on_success(self, results):
        inst = self._inst()
        self._sys(f"✓ Instance '{inst.name}' restarted.")
        inst.write({**inst._running_confirmed_vals(), "status": "ok"})

    async def on_failure(self, results, errors):
        """Mark the instance in error, unless nothing was touched.

        A step refused for a rebuild left unapplied
        (``COMPOSE_OP_EXIT_UNAPPLIED``) ran nothing: the instance stays as
        it was, and so does its status.
        """
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        step = (results or {}).get("Restart containers") or {}
        if inst and step.get("exit_status") != COMPOSE_OP_EXIT_UNAPPLIED:
            inst.write({"status": "error"})
