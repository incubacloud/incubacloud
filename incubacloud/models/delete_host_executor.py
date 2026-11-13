"""
Delete Host Executor
--------------------
Stops Traefik and removes the ~/traefik directory via
``ansible/playbooks/host_teardown.yml``, then archives the host record
so it disappears from the dashboard while preserving job history.

Prerequisites (enforced at the controller level):
  - Host must have zero active instances.
  - Host must not run the metrics central (also checked before the run,
    for a job queued before the central moved there).
"""

from .abstract_executor import JobRefused
from .ansible_executor import AnsibleExecutor


class DeleteHostExecutor(AnsibleExecutor):
    _job_type = "delete_host"
    _playbook = "playbooks/host_teardown.yml"

    def _host(self):
        return self.job.host_id

    def pre_run_checks(self):
        """Refuse to tear down the server the metrics central runs on.

        The route refuses before queuing; this covers a job queued before
        the central was deployed there, or retried. The teardown removes
        the server's proxy before archiving, so refusing only at the
        archive would leave the central unreachable.

        :raises JobRefused: naming the host and the ways out
        """
        super().pre_run_checks()
        host = self._host()
        central = self.env["cloud.settings"].sudo()._live_central_host()
        if host and host == central:
            problem = self.env["cloud.settings"]._central_host_refusal(host)
            self._write_system_direct(f"⚠ {problem}")
            raise JobRefused(problem)

    async def on_success(self, results):
        host = self._host()
        name = host.name if host else "?"
        self._sys(f"✓ Host '{name}' cleaned up. Archiving record.")
        if host:
            host.write({'active': False})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
