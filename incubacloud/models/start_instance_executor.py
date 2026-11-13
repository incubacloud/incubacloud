from .abstract_executor import (
    COMPOSE_OP_EXIT_PREVIOUS_BUILD,
    COMPOSE_OP_EXIT_UNAPPLIED,
    COMPOSE_OP_EXIT_UNCHECKED,
    AbstractSSHExecutor,
)


class StartInstanceExecutor(AbstractSSHExecutor):
    """Start a stopped doodba instance via docker compose up -d."""

    _job_type = "start_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    def get_commands(self):
        inst = self._inst()
        return [
            (
                "Start containers",
                self.run_script(
                    "compose_op.sh", [self._inst_dir(inst), "up"],
                ),
                {"continue_on": (
                    COMPOSE_OP_EXIT_UNCHECKED, COMPOSE_OP_EXIT_PREVIOUS_BUILD,
                )},
            ),
        ]

    def parse_results(self, results):
        """Fail-closed like the base, except when only the check failed.

        ``compose_op.sh`` exits ``COMPOSE_OP_EXIT_UNCHECKED`` when the
        containers started but the steps that follow could not be
        checked, and ``COMPOSE_OP_EXIT_PREVIOUS_BUILD`` when a rebuild
        left its files and image unapplied and the containers the instance
        had were started instead of recreated: the job succeeds, with a
        warning in its log. ``COMPOSE_OP_EXIT_UNAPPLIED`` fails it saying
        why and what to do (#96).
        """
        refusal = self._unapplied_refusal(results, "Start containers")
        if refusal:
            return [refusal]
        results = self._accept_previous_build(results, "Start containers")
        return super().parse_results(
            self._accept_unchecked_compose_op(results, "Start containers"),
        )

    async def on_success(self, results):
        inst = self._inst()
        self._sys(f"✓ Instance '{inst.name}' started.")
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
        step = (results or {}).get("Start containers") or {}
        if inst and step.get("exit_status") != COMPOSE_OP_EXIT_UNAPPLIED:
            inst.write({"status": "error"})
