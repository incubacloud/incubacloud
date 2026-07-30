"""
Delete Host Executor
--------------------
Stops Traefik and removes the ~/traefik directory via
``ansible/playbooks/host_teardown.yml``, then archives the host record
so it disappears from the dashboard while preserving job history.

Prerequisites (enforced at the controller level):
  - Host must have zero active instances.
"""

from .ansible_executor import AnsibleExecutor


class DeleteHostExecutor(AnsibleExecutor):
    _job_type = "delete_host"
    _playbook = "playbooks/host_teardown.yml"

    def _host(self):
        return self.job.host_id

    async def on_success(self, results):
        host = self._host()
        name = host.name if host else "?"
        self._sys(f"✓ Host '{name}' cleaned up. Archiving record.")
        if host:
            host.write({'active': False})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
