import asyncio
import time

from .abstract_executor import JobRefused
from .delete_instance_executor import DeleteInstanceExecutor


class MoveRollbackCleanupExecutor(DeleteInstanceExecutor):
    """Clean up the half-built destination after a rolled-back move.

    Runs on the TARGET host, and on no other: it refuses, before it
    connects, a host that is not the target of the move being rolled
    back, or one the instance lives on (``_refusal``). Waits for all
    move-chain jobs to reach a terminal state (cooperative cancel
    propagation), then tears down whatever the deploy step built (docker
    compose down -v + rm -rf). Short of that refusal, never fails the
    job — connection errors or partial cleanup are logged as warnings,
    not failures, so the ``start_instance`` on the source is never
    blocked by a cleanup problem on the target.

    On success clears all move-related markers so the instance returns
    to a clean state with ``host_id`` still pointing at the source.
    """

    _job_type = "move_rollback_cleanup"
    _retry_on_connection_loss = False

    # Queued for the move's target while the instance still lives on its
    # source: the queue must not send it where the instance lives. It
    # did, and removed the source's copy (instance 2807, 28-oct-2026).
    _pinned_to_host = True

    # ``before_execute`` below can wait up to ten minutes for the move
    # chain to settle. The per-host lock is taken before it, so holding
    # it here would park every build and teardown on the target host
    # behind a wait that does nothing to the host.
    _takes_host_build_lock = False

    # Tears down the half-built copy on the TARGET host while the
    # instance keeps living on the source: the record's lifecycle state
    # must not move. (This class also replaces ``before_execute``
    # entirely, but the flag states the intent for whoever edits it.)
    _owns_instance_lifecycle = False

    MOVE_CHAIN_TERMINAL_TIMEOUT = 600  # 10 minutes

    def _refusal(self, inst):
        """Say why this teardown may not run on the job's host, or None.

        It removes a copy of the instance: only the half-built one on the
        target of the move being rolled back, never one where the instance
        lives, and only for the rollback that queued it. A failed cleanup
        retried later, once a new move to the same host had set the
        markers again, waited for that move, then removed its copy and its
        markers.

        :param inst: the ``cloud.instance``, as read now
        :return: the refusal, for the job log, or None
        :rtype: str | None
        """
        host = self.job.host_id
        if host == inst.host_id:
            return (
                f"Refused: the instance lives on {host.name}. This cleanup "
                "only removes the copy a rolled-back move left on its target."
            )
        if not inst.move_rollback_in_progress:
            return (
                "Refused: no rollback of a move of this instance is in "
                "progress, and this cleanup belongs to one."
            )
        if host != inst.move_target_host_id:
            return (
                f"Refused: {host.name} is not the target of a move of this "
                "instance being rolled back."
            )
        chain = (self.job.payload or {}).get("move_chain_job_ids")
        if not chain or chain != inst.move_chain_job_ids:
            return (
                "Refused: this cleanup belongs to an earlier rollback, not "
                "to the one in progress."
            )
        return None

    def _check_still_due(self, env):
        """Read the instance again and refuse what no longer holds.

        :param env: an environment on a cursor of its own, which sees
            what other transactions committed
        :raises RuntimeError: the instance is gone, or the cutover
            happened after all
        :raises JobRefused: see ``_refusal``
        """
        inst = env['cloud.instance'].browse(self.job.instance_id.id)
        if not inst.exists():
            self._sys("✗ Instance no longer exists — nothing to clean up.")
            raise RuntimeError("Instance deleted — cleanup aborted.")
        # Cutover already completed — host_id flipped. The move
        # succeeded despite our cancellation; abort to avoid destroying
        # a live instance.
        if inst.host_id != inst.move_origin_host_id:
            self._sys(
                "✗ Cutover already completed (host_id flipped). "
                "Aborting cleanup — the instance is live on the "
                "target host."
            )
            raise RuntimeError("Cleanup aborted: cutover already completed.")
        refusal = self._refusal(inst)
        if refusal:
            raise JobRefused(refusal)
        return inst

    def pre_run_checks(self):
        """Refuse any host but the target of the move being rolled back.

        Before the teardown's own checks and before connecting: a refused
        cleanup reaches no host. It fails, and the watchdog
        (``cloud.instance.cron_recover_stuck_moves``) ends the rollback
        and says the target may need cleaning by hand.

        :raises JobRefused: see ``_refusal``
        """
        inst = self.job.instance_id
        refusal = self._refusal(inst) if inst else None
        if refusal:
            raise JobRefused(refusal)
        super().pre_run_checks()

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
            with self._durable_env() as env:
                # Read again after each wait: what the markers say now.
                inst = self._check_still_due(env)
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
        # Ten minutes later, and before anything is removed: the rollback
        # may have ended meanwhile, and a new move begun.
        with self._durable_env() as env:
            self._check_still_due(env)

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
            target = inst.move_target_host_id
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
            inst._on_move_rolled_back(target_host=target)
        self._sys("✓ Rollback cleanup complete. Markers cleared.")
