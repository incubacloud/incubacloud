import asyncio
import time

from .delete_instance_executor import DeleteInstanceExecutor


class MoveRollbackCleanupExecutor(DeleteInstanceExecutor):
    """Clean up the half-built destination after a rolled-back move.

    Runs on the TARGET host. Waits for all move-chain jobs to reach a
    terminal state (cooperative cancel propagation), then tears down
    whatever the deploy step built (docker compose down -v + rm -rf).
    Never fails the job — connection errors or partial cleanup are
    logged as warnings, not failures, so the ``start_instance`` on the
    source is never blocked by a cleanup problem on the target.

    On success clears all move-related markers so the instance returns
    to a clean state with ``host_id`` still pointing at the source.
    """

    _job_type = "move_rollback_cleanup"
    _retry_on_connection_loss = False

    MOVE_CHAIN_TERMINAL_TIMEOUT = 600  # 10 minutes

    async def before_execute(self, transport):
        """Wait for all move-chain jobs to reach a terminal state.

        Polls the DB every 10 s. If ``host_id`` flips during the wait
        (cutover completed despite our cancellation) the executor aborts
        so it never destroys a live instance on the target.

        The ``_check_cancel`` in the outer loop still runs every
        ``sleep_interval``, so a second rollback or a manual cancellation
        breaks the wait immediately.
        """
        active_states = self.env['cloud.job']._active_states
        deadline = time.monotonic() + self.MOVE_CHAIN_TERMINAL_TIMEOUT
        while time.monotonic() < deadline:
            with self.job.env.registry.cursor() as cr:
                inst = self.job.env(cr=cr)['cloud.instance'].browse(
                    self.job.instance_id.id,
                )
                if not inst.exists():
                    self._sys(
                        "✗ Instance no longer exists — nothing to clean up."
                    )
                    raise RuntimeError("Instance deleted — cleanup aborted.")
                # Cutover already completed — host_id flipped. The move
                # succeeded despite our cancellation; abort to avoid
                # destroying a live instance.
                if inst.host_id != inst.move_origin_host_id:
                    self._sys(
                        "✗ Cutover already completed (host_id flipped). "
                        "Aborting cleanup — the instance is live on the "
                        "target host."
                    )
                    raise RuntimeError(
                        "Cleanup aborted: cutover already completed."
                    )
                chain_jobs = inst._get_move_chain_jobs()
                active = chain_jobs.filtered(
                    lambda j: j.state in active_states,
                )
            if not active:
                self._sys("✓ All move-chain jobs reached terminal state.")
                return
            self._sys(
                "Waiting for %d move-chain job(s) to finish..."
                % len(active),
            )
            await asyncio.sleep(10)

        self._sys(
            "⚠ Timeout waiting for move-chain jobs to terminate. "
            "Proceeding with cleanup anyway (best effort)."
        )

    def parse_results(self, results):
        """Never fail — log exit codes as system messages instead."""
        for label, data in results.items():
            if data.get('exit_status', 1) != 0:
                self._sys(
                    "⚠ '%s' exited with status %s (non-fatal)."
                    % (label, data.get('exit_status')),
                )
        return []

    async def on_success(self, results):
        inst = self.job.instance_id
        if inst.exists():
            inst.write({
                'move_origin_host_id': False,
                'move_target_host_id': False,
                'move_chain_job_ids': False,
                'move_rollback_in_progress': False,
            })
            # Dismiss any stale move_stuck alert that may have been
            # raised by a previous cron recovery pass.
            self.env['cloud.alert'].search([
                ('instance_id', '=', inst.id),
                ('code', '=', 'move_stuck'),
                ('state', '=', 'active'),
            ]).write({'state': 'dismissed'})
        self._sys("✓ Rollback cleanup complete. Markers cleared.")
