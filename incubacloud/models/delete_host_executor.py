"""
Delete Host Executor
--------------------
Stops Traefik and removes the ~/traefik directory, then archives the host
record so it disappears from the dashboard while preserving job history.

Prerequisites (enforced at the controller level):
  - Host must have zero active instances.
"""

from .abstract_executor import AbstractSSHExecutor


class DeleteHostExecutor(AbstractSSHExecutor):
    _job_type = "delete_host"

    def _host(self):
        return self.job.host_id

    def get_commands(self):
        return [
            (
                "Stop Traefik",
                "if [ -f ~/traefik/inverseproxy.yaml ]; then"
                "  cd ~/traefik && docker compose -p inverseproxy -f inverseproxy.yaml down;"
                "else"
                "  echo 'Traefik not deployed, skipping.';"
                "fi",
            ),
            (
                "Remove Traefik directory",
                "rm -rf ~/traefik",
            ),
        ]

    async def on_success(self, results):
        host = self._host()
        name = host.name if host else "?"
        self._sys(f"✓ Host '{name}' cleaned up. Archiving record.")
        if host:
            host.write({'active': False})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
