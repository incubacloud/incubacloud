from odoo import fields

from .abstract_executor import AbstractSSHExecutor, JobRefused
from .delete_instance_executor import DeleteInstanceExecutor


class MoveCutoverExecutor(AbstractSSHExecutor):
    """Verify the destination instance is healthy, then cut over.

    Runs on the destination host, after the restore, near the end of a
    host move. It waits for Odoo to answer its health endpoint; if the
    destination never becomes healthy the job FAILS, which breaks the
    move chain so the source is never frozen and the instance stays
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
        # The cut began when the source's stop started to drain it
        # (``StopInstanceExecutor``, ``move_cut``) and ends here, with
        # the destination answering healthy.
        started = inst.move_cut_started_at
        cut_seconds = (
            (fields.Datetime.now() - started).total_seconds() if started else None
        )
        # With ``sudo``: the markers are the platform's
        # (``cloud.instance._check_guarded_vals``).
        inst.sudo().write({
            'host_id': target.id,
            'status': 'ok',
            **inst._running_confirmed_vals(),
            'move_origin_host_id': False,
            'move_target_host_id': False,
            'move_chain_job_ids': False,
            'move_cut_started_at': False,
            **(
                {'move_last_cut_seconds': cut_seconds}
                if cut_seconds is not None else {}
            ),
        })
        self._sys(f"✓ Cut over to host '{target.name}'.")
        if cut_seconds is not None:
            self._sys(f"The instance was unavailable for {cut_seconds:.0f} s.")
            # Over ``move_cut_alert_seconds`` the instance's
            # ``move_slow_cut`` is raised; the next cut under it closes it.
            limit = self.env["cloud.settings"].sudo()._get().move_cut_alert_seconds
            Alert = self.env["cloud.alert"].sudo()
            if limit and cut_seconds > limit:
                Alert.raise_alert(
                    "move_slow_cut",
                    self.env._(
                        "The move of %(name)s kept it down %(seconds)d s, "
                        "over the %(limit)d s budget.",
                        name=inst.name, seconds=round(cut_seconds), limit=limit,
                    ),
                    level="warning",
                    instance=inst,
                )
            else:
                Alert.resolve_alert("move_slow_cut", instance=inst)
        # Hook: SaaS re-points DNS at the new host IP and finalizes the
        # tenant. Core is a no-op.
        inst._on_move_cutover(origin, cut_seconds=cut_seconds)
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
        # freeze of the old copy that closes the chain).
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
        # Before the markers that name the chain go.
        outcome = inst._move_outcome("cancelled")
        # With ``sudo``: the markers are the platform's.
        inst.sudo().write({
            "move_origin_host_id": False,
            "move_target_host_id": False,
            "move_chain_job_ids": False,
        })
        inst._on_move_rolled_back(target_host=target, outcome=outcome)


class MoveCleanupSourceExecutor(DeleteInstanceExecutor):
    """Remove the copy a move froze on the old host.

    Reuses ``delete_instance``'s teardown (compose down -v + rm dir) on
    the host of the frozen copy (``cloud.instance.move_frozen_source_host_id``,
    set by ``move_freeze_source``) but, unlike ``delete_instance``, does
    NOT archive the instance record — the instance lives on another host.
    Queued once the copy's time is up (``cron_clean_frozen_move_sources``),
    ahead of a new move or of the instance's delete, or by hand
    (``action_clean_frozen_copy``). It removes the copy of an archived
    instance too: the copy outlives the record's archiving.
    """

    _job_type = "move_cleanup_source"

    # The instance lives on the destination host by now: this teardown
    # touches the abandoned copy only, so it must not move the record's
    # lifecycle state.
    _owns_instance_lifecycle = False

    # The old host, named on purpose: a retry sent where the instance
    # lives now would remove the live copy.
    _pinned_to_host = True

    # An archived instance keeps its frozen copy until this removes it.
    _runs_on_archived_instance = True

    def pre_run_checks(self):
        """Refuse the host the instance lives on, or one with no frozen copy.

        Before the teardown's own checks and before connecting: a refused
        cleanup reaches no host. The host of the frozen copy may be the
        target of a new move, which queues this first: the copy goes
        before the move builds there.

        :raises JobRefused: when the job's host holds a copy in use, or
            no copy frozen by a move
        """
        inst = self._inst()
        host = self.job.host_id
        if inst and host == inst.host_id:
            raise JobRefused(
                f"Refused: the instance lives on {host.name}. This cleanup "
                "only removes the copy a finished move left on its old host."
            )
        if inst and host != inst.move_frozen_source_host_id:
            raise JobRefused(
                f"Refused: no frozen copy of this instance is kept on "
                f"{host.name}. This cleanup only removes the copy a finished "
                "move left on its old host."
            )
        super().pre_run_checks()

    def get_commands(self):
        """The teardown, whatever the record says about its own removal.

        ``removal_finalized`` speaks of the instance's own copy; the
        frozen one is another, still on its host. Then what the layers
        remove with it (``cloud.instance._frozen_copy_cleanup_steps``).
        """
        inst = self._inst()
        if not inst.exists():
            self._sys("Instance already removed; nothing to do.")
            return []
        return [
            *self._teardown_commands(inst),
            *inst._frozen_copy_cleanup_steps(),
        ]

    async def on_success(self, results):
        self._sys("✓ Old host copy removed after move.")
        inst = self._inst()
        host = self._host()
        if inst.exists() and inst.move_frozen_source_host_id == host:
            # With ``sudo``: the fields are the platform's.
            inst.sudo().write({
                "move_frozen_source_host_id": False,
                "move_frozen_until": False,
            })
        # The old host no longer holds the instance: drop it from that
        # host's label map.
        host.refresh_observability_labels(reason="move cleanup")

    async def on_failure(self, results, errors):
        # A cleanup failure must NOT flip the instance to error: the move
        # already succeeded and the instance is live on the destination.
        # Just log; the frozen copy stays recorded for the next attempt.
        for err in errors:
            self._sys(f"✗ Source cleanup: {err}")
