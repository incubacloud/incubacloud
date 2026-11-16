import time

from odoo import fields

from .abstract_executor import AbstractSSHExecutor


class StopInstanceExecutor(AbstractSSHExecutor):
    """Stop a running doodba instance via docker compose stop."""

    _job_type = "stop_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def before_execute(self, transport):
        """Stamp the start of a move's cut on the stop of its source.

        ``move_cut`` marks that stop (``cloud.instance.move_to_host``):
        from here the instance drains and stays unavailable until the
        destination's cutover, which measures the cut from this stamp.
        Written on a cursor of its own, so the stamp is there whatever
        becomes of this job's transaction.

        The scripts go up first (the pattern ``_upload_scripts``
        documents; ``_async_entry``'s call is then a no-op), and anything
        a layer stages goes before this method: the stamp is followed by
        the drain itself. Stamped before the upload, the cut counted ~4.5 s
        the instance was still serving (4-nov-2026). The cut's parts
        (``move_cut_parts``) start over with it, so a chain that failed
        without a rollback leaves none to the next.
        """
        await super().before_execute(transport)
        if not (self.job.payload or {}).get("move_cut"):
            return
        list(self.get_commands())
        await self._upload_scripts(transport)
        inst = self._inst()
        with self._durable_env() as env:
            # With ``sudo``: the stamp is the platform's
            # (``cloud.instance._check_guarded_vals``).
            env["cloud.instance"].browse(inst.id).sudo().write({
                "move_cut_started_at": fields.Datetime.now(),
                "move_cut_parts": {"started": time.time(), "jobs": {}},
            })

    def get_commands(self):
        """Return the command(s) to stop a running doodba instance.

        By default stops all compose services. When ``payload.services``
        is a non-empty list of service names, only those services are
        stopped — used by ``move_to_host`` to quiesce ``odoo`` while
        leaving ``db`` and ``backup`` alive for the subsequent
        ``backup_create``/``backup_download`` steps.
        """
        inst = self._inst()
        payload = self.job.payload or {}
        services = [s for s in (payload.get('services') or []) if s]
        return [
            (
                "Stop containers",
                self.run_script(
                    "compose_op.sh",
                    [self._inst_dir(inst), "stop", *services],
                ),
            ),
        ]

    async def on_success(self, results):
        inst = self._inst()
        self._sys(f"✓ Instance '{inst.name}' stopped.")
        # A move stops its destination's copy before the cut; the instance
        # itself still runs on its source until that stops too.
        if not (self.job.payload or {}).get("move_target"):
            inst.write({"running": False})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
