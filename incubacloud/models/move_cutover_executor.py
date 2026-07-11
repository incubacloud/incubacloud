from .abstract_executor import AbstractSSHExecutor
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

    def _inst(self):
        return self.job.instance_id

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)
        # Poll the in-container health endpoint for up to ~90s so a
        # freshly restored stack has time to come up before cutover.
        return [
            (
                "health_wait",
                f"cd {d} && for i in $(seq 1 30); do "
                f"if docker compose exec -T odoo curl -sf --max-time 10 "
                f"http://localhost:8069/web/health >/dev/null 2>&1; then "
                f"echo 'health:ok'; exit 0; fi; sleep 3; done; "
                f"echo 'health:timeout'; exit 1",
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
        inst.write({
            'host_id': target.id,
            'status': 'ok',
            'running': True,
            'move_origin_host_id': False,
            'move_target_host_id': False,
        })
        self._sys(f"✓ Cut over to host '{target.name}'.")
        # Hook: SaaS re-points DNS at the new host IP and finalizes the
        # tenant. Core is a no-op.
        inst._on_move_cutover(origin)

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


class MoveCleanupSourceExecutor(DeleteInstanceExecutor):
    """Remove the old host copy after a successful move.

    Reuses ``delete_instance``'s teardown (compose down -v + rm dir) on
    the SOURCE host but, unlike ``delete_instance``, does NOT archive the
    instance record — by now the instance lives on the destination host.
    Runs as the last step of the move chain, so it only fires when the
    destination was verified and cut over.
    """

    _job_type = "move_cleanup_source"

    async def on_success(self, results):
        self._sys("✓ Old host copy removed after move.")

    async def on_failure(self, results, errors):
        # A cleanup failure must NOT flip the instance to error: the move
        # already succeeded and the instance is live on the destination.
        # Just log; the leftover source directory can be reclaimed later.
        for err in errors:
            self._sys(f"✗ Source cleanup: {err}")
