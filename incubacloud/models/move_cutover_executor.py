import inspect
import logging
import time

from odoo import fields

from .abstract_executor import AbstractSSHExecutor, JobRefused
from .delete_instance_executor import DeleteInstanceExecutor


def _takes_keyword(method, name):
    """Whether *method* can be called with the keyword argument *name*.

    :param method: a bound method
    :param str name: the argument
    :rtype: bool
    """
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        or (param.name == name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ))
        for param in inspect.signature(method).parameters.values()
    )


#: The part of the cut each job of a move chain is (16-nov-2026). A job of
#: another type (a layer's step inside the cut) is a part of its own name.
CUT_PART_BY_JOB_TYPE = {
    "stop_instance": "drain",
    "move_dns_repoint": "dns",
    "move_fingerprint": "fingerprint",
    "backup_download": "dump",
    "restore_instance": "restore",
    "move_cutover": "health",
}

#: The parts every move of this core has: without one of them, the
#: breakdown says ``incomplete`` (a chain queued by an earlier core, an
#: attempt that could not note its part). ``dns`` is a layer's.
CUT_PARTS_EXPECTED = ("drain", "fingerprint", "dump", "restore", "health")

#: The restore's step that starts Odoo, a part of its own (``start``).
CUT_RESTORE_START_STEP = "Start Odoo service"


def _number(value):
    """Whether *value* is a number noted in the parts (not a bool).

    :rtype: bool
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def reduce_cut_parts(parts, ended):
    """Reduce what the jobs of a move noted of its cut to seconds by part.

    *parts* is ``cloud.instance.move_cut_parts``: ``started`` (the stamp,
    ``time.time()``) and ``jobs`` (``AbstractSSHExecutor._note_move_cut_part``).
    Every interval is cut to ``[started, ended]``: the source's stop
    connects and uploads before the stamp, and that is not the cut. Per
    job: its connection and script upload are ``overhead``; the rest is
    its part (``CUT_PART_BY_JOB_TYPE``), save the restore's, whose
    ``before_execute`` is the ``relay`` of the archive and whose start of
    Odoo is ``start``. ``queue`` is the time between one job's end and the
    next one's start. ``other`` is what the parts leave of ``total``.

    Never raises on what it is given: whatever is missing or malformed is
    left out and the result says ``incomplete``.

    :param parts: the noted parts, or anything else
    :param float ended: the end of the cut, ``time.time()``
    :return: ``{part: seconds, 'total', 'other', 'incomplete'[, 'resumed']}``,
        or None when there is no start to measure from
    :rtype: dict | None
    """
    if not isinstance(parts, dict) or not _number(parts.get("started")):
        return None
    started = parts["started"]
    out = {}
    incomplete = False
    resumed = set()

    def credit(key, begin, end):
        """Credit the share of ``[begin, end]`` that falls inside the cut."""
        if not (_number(begin) and _number(end)):
            return
        span = min(end, ended) - max(begin, started)
        if span > 0:
            out[key] = out.get(key, 0.0) + span

    jobs = parts.get("jobs")
    entries = [
        entry for entry in (jobs.values() if isinstance(jobs, dict) else ())
        if isinstance(entry, dict) and _number(entry.get("end"))
    ]
    if isinstance(jobs, dict) and len(entries) != len(jobs):
        incomplete = True
    entries.sort(key=lambda entry: (
        entry["start"] if _number(entry.get("start")) else entry["end"]
    ))
    previous = started
    for entry in entries:
        kind = entry.get("type") or "unknown"
        part = CUT_PART_BY_JOB_TYPE.get(kind, kind)
        if entry.get("resumed"):
            resumed.add(part)
        start, end = entry.get("start"), entry["end"]
        steps = entry.get("steps")
        steps = steps if isinstance(steps, dict) else {}
        if not _number(start):
            # Resumed with no marks of the attempt that began it: what its
            # steps took is all there is.
            incomplete = True
            boot = 0.0
            if kind == "restore_instance":
                boot = steps.get(CUT_RESTORE_START_STEP)
                boot = boot if _number(boot) and boot > 0 else 0.0
                if boot:
                    out["start"] = out.get("start", 0.0) + boot
            out[part] = out.get(part, 0.0) + sum(
                value for value in steps.values() if _number(value)
            ) - boot
            previous = max(previous, end)
            continue
        connected = entry.get("connected", start)
        prepared = entry.get("prepared", connected)
        uploaded = entry.get("uploaded", prepared)
        credit("queue", previous, start)
        credit("overhead", start, connected)
        credit("overhead", prepared, uploaded)
        if kind == "restore_instance":
            credit("relay", connected, prepared)
            credit("restore", uploaded, end)
            boot = steps.get(CUT_RESTORE_START_STEP)
            if _number(boot) and boot > 0:
                boot = min(boot, out.get("restore", 0.0))
                out["restore"] = out.get("restore", 0.0) - boot
                out["start"] = out.get("start", 0.0) + boot
        else:
            credit(part, connected, prepared)
            credit(part, uploaded, end)
        previous = max(previous, end)
    if any(part not in out for part in CUT_PARTS_EXPECTED):
        incomplete = True
    total = max(ended - started, 0.0)
    result = {key: round(value, 2) for key, value in out.items()}
    result["total"] = round(total, 2)
    result["other"] = round(total - sum(out.values()), 2)
    result["incomplete"] = incomplete
    if resumed:
        result["resumed"] = sorted(resumed)
    return result


class MoveCutoverExecutor(AbstractSSHExecutor):
    """Verify the destination instance is healthy, then cut over.

    Runs on the destination host, after the restore, near the end of a
    host move. It waits for Odoo to answer its health endpoint; if the
    destination never becomes healthy the job FAILS, which breaks the
    move chain so the source is never frozen and the instance stays
    recoverable. On success it flips ``host_id`` to the destination,
    clears the in-flight marker and fires ``_on_move_cutover`` (a layer
    may re-point a DNS record it manages).
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
        # Poll the in-container health endpoint every 0.5 s for up to
        # 120 s so a freshly restored stack has time to come up before
        # cutover (``move_health_wait.sh`` gives the values' reasons).
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
        # What each job of the cut noted, this one's included
        # (``_dispatch_outcome`` notes it before this hook). Measurement
        # only: a breakdown that cannot be made never stops the cutover.
        try:
            breakdown = reduce_cut_parts(inst.move_cut_parts, time.time())
        except Exception:  # noqa: BLE001 — logged, never fatal
            logging.getLogger(__name__).warning(
                "[job %s] could not reduce the parts of the cut",
                self.job.id, exc_info=True,
            )
            breakdown = None
        # From the chain the markers name, before they are cleared.
        trigger = inst._move_trigger()
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
            'move_cut_parts': False,
            **(
                {'move_last_cut_seconds': cut_seconds}
                if cut_seconds is not None else {}
            ),
            # Cleared when there is none, so a move that noted nothing does
            # not show the breakdown of the one before.
            'move_last_cut_breakdown': breakdown or False,
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
        # Hook: a layer may re-point DNS at the new host IP and finish
        # what it owns. Core is a no-op. An override written before
        # ``trigger`` (a layer deployed apart from this core) is called
        # without it rather than failing a cutover that worked.
        hook = inst._on_move_cutover
        extra = {"trigger": trigger} if _takes_keyword(hook, "trigger") else {}
        hook(origin, cut_seconds=cut_seconds, **extra)
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
        (a layer's record) is told the move was undone. The markers of a
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
            # A copy whose freeze failed, possibly running until now.
            self.env["cloud.alert"].sudo().resolve_alert(
                "move_freeze_failed", instance=inst,
            )
        # The old host no longer holds the instance: drop it from that
        # host's label map.
        host.refresh_observability_labels(reason="move cleanup")

    async def on_failure(self, results, errors):
        # A cleanup failure must NOT flip the instance to error: the move
        # already succeeded and the instance is live on the destination.
        # Just log; the frozen copy stays recorded for the next attempt.
        for err in errors:
            self._sys(f"✗ Source cleanup: {err}")
