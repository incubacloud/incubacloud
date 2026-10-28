from .abstract_executor import AbstractSSHExecutor, JobRefused
from .delete_instance_executor import DeleteInstanceExecutor


class MoveCutoverExecutor(AbstractSSHExecutor):
    """Verify the destination instance is healthy, then cut over.

    Runs on the destination host as the second-to-last step of a host
    move. It waits for Odoo to answer its health endpoint; if the
    destination never becomes healthy the job FAILS, which breaks the
    move chain so the source teardown never runs and the instance stays
    recoverable. On success it flips ``host_id`` to the destination,
    clears the in-flight marker and fires ``_on_move_cutover`` (SaaS
    re-points the managed DNS record).
    """

    _job_type = "move_cutover"

    # Give a just-restored stack time to boot before deciding it's
    # unhealthy: retry transient connection blips rather than failing
    # the cutover (and aborting the whole move) on the first one.
    _retry_on_connection_loss = True

    # The move's target, named on purpose: a retry sent where the
    # instance lives would check the source and cut over to it.
    _pinned_to_host = True

    def _inst(self):
        return self.job.instance_id

    def pre_run_checks(self):
        """Refuse a cutover that no longer belongs to a move in progress.

        It hands the instance to the copy on the job's host. Only the
        target of the move under way may have it — or the host it lives
        on already, for a run that queue_job repeats after its success.
        Being rolled back, or once undone, the move is over: a retry of
        its cutover would hand the instance to a copy of the data taken
        before the rollback.

        :raises JobRefused: when the move it belongs to is over
        """
        inst = self._inst()
        host = self.job.host_id
        if inst and inst.move_rollback_in_progress:
            raise JobRefused(
                "Refused: the move this cutover belongs to is being rolled back."
            )
        if inst and host != inst.host_id and host != inst.move_target_host_id:
            raise JobRefused(
                f"Refused: no move of this instance to {host.name} is in progress."
            )
        super().pre_run_checks()

    def get_commands(self):
        inst = self._inst()
        # Poll the in-container health endpoint for up to ~90s so a
        # freshly restored stack has time to come up before cutover.
        return [
            (
                "health_wait",
                self.run_script("move_health_wait.sh", [self._inst_dir(inst)]),
            ),
        ]

    def parse_results(self, results):
        data = results.get("health_wait", {})
        if (data.get('exit_status', 1) != 0
                or 'health:ok' not in data.get('stdout', '')):
            return ["Destination instance did not become healthy in time."]
        return []

    async def on_success(self, results):
        inst = self._inst()
        target = self.job.host_id
        origin = inst.move_origin_host_id
        # With ``sudo``: the markers are the platform's
        # (``cloud.instance._check_guarded_vals``).
        inst.sudo().write({
            'host_id': target.id,
            'status': 'ok',
            **inst._running_confirmed_vals(),
            'move_origin_host_id': False,
            'move_target_host_id': False,
            'move_chain_job_ids': False,
        })
        self._sys(f"✓ Cut over to host '{target.name}'.")
        # Hook: SaaS re-points DNS at the new host IP and finalizes the
        # tenant. Core is a no-op.
        inst._on_move_cutover(origin)
        self._call_addon_hook(inst, "_after_restore_hook", "move")
        # Nothing re-pointed DNS: say so loudly. The move succeeded but
        # the instance answers on a new IP, so it is unreachable until a
        # human updates the record — silence here reads as a broken move.
        if not inst._move_dns_is_managed():
            self._sys(
                f"⚠ DNS is not managed by this panel — re-point "
                f"{inst.domain or 'the instance domain'} to "
                f"{target.ip_address or 'the new host IP'} or the "
                f"instance stays unreachable."
            )
            inst._notify_move_dns_repoint(target)
        # The instance now belongs to the target host: its label map must
        # learn about it, and the source's must forget it (done by the
        # cleanup step that closes the chain).
        target.refresh_observability_labels(reason="move cutover")

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
        self._sys(
            "Cutover aborted — the source is untouched; an operator can "
            "roll back this move."
        )

    def on_cancelled_queued(self):
        """A move cancelled while its chain waited is a move that never was.

        ``move_to_host`` sets the markers (origin, target, chain) before
        queueing; the chain cancelled whole in the queue, nothing on any
        host changed, so the markers go and whoever inherits the record
        (the SaaS tenant) is told the move was undone. The markers of a
        move whose rollback is under way are the rollback's to clear.
        """
        inst = self._inst()
        if not inst or not inst.move_origin_host_id or inst.move_rollback_in_progress:
            return
        if str(self.job.id) not in (inst.move_chain_job_ids or "").split(","):
            return
        target = inst.move_target_host_id
        # With ``sudo``: the markers are the platform's.
        inst.sudo().write({
            "move_origin_host_id": False,
            "move_target_host_id": False,
            "move_chain_job_ids": False,
        })
        inst._on_move_rolled_back(target_host=target)


class MoveCleanupSourceExecutor(DeleteInstanceExecutor):
    """Remove the old host copy after a successful move.

    Reuses ``delete_instance``'s teardown (compose down -v + rm dir) on
    the SOURCE host but, unlike ``delete_instance``, does NOT archive the
    instance record — by now the instance lives on the destination host.
    Runs as the last step of the move chain, so it only fires when the
    destination was verified and cut over.
    """

    _job_type = "move_cleanup_source"

    # The instance lives on the destination host by now: this teardown
    # touches the abandoned copy only, so it must not move the record's
    # lifecycle state.
    _owns_instance_lifecycle = False

    # The old host, named on purpose: a retry sent where the instance
    # lives now would remove the live copy.
    _pinned_to_host = True

    def pre_run_checks(self):
        """Refuse the host the instance lives on, or the one it moves to.

        Before the teardown's own checks and before connecting: a refused
        cleanup reaches no host. Before the cutover the source is still
        the instance's host; once moved back, the old source may be the
        target of the new move, being built.

        :raises JobRefused: when the job's host holds a copy in use
        """
        inst = self._inst()
        host = self.job.host_id
        if inst and host == inst.host_id:
            raise JobRefused(
                f"Refused: the instance lives on {host.name}. This cleanup "
                "only removes the copy a finished move left on its old host."
            )
        if inst and host == inst.move_target_host_id:
            raise JobRefused(
                f"Refused: the instance is moving to {host.name}. This "
                "cleanup only removes the copy a finished move left on its "
                "old host."
            )
        super().pre_run_checks()

    async def on_success(self, results):
        self._sys("✓ Old host copy removed after move.")
        # This job runs on the SOURCE host, which no longer hosts the
        # instance: drop it from that host's label map.
        self._host().refresh_observability_labels(reason="move cleanup")

    async def on_failure(self, results, errors):
        # A cleanup failure must NOT flip the instance to error: the move
        # already succeeded and the instance is live on the destination.
        # Just log; the leftover source directory can be reclaimed later.
        for err in errors:
            self._sys(f"✗ Source cleanup: {err}")
