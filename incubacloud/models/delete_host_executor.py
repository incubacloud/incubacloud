"""
Delete Host Executor
--------------------
Stops the metrics agents and Traefik and removes their directories via
``ansible/playbooks/host_teardown.yml``, then takes the panel's access
off the server: its key, and the operator account and sudo grant that
hardening made for it. Then it archives the host record so it
disappears from the dashboard while preserving job history.

Prerequisites (enforced at the controller level):
  - Host must have zero active instances.
  - Host must not run the metrics central (also checked before the run,
    for a job queued before the central moved there).
"""

from .abstract_executor import JobRefused
from .ansible_executor import AnsibleExecutor
from .host_hardening_executor import DEFAULT_HARDENED_USER


class DeleteHostExecutor(AnsibleExecutor):
    _job_type = "delete_host"
    _playbook = "playbooks/host_teardown.yml"

    def _host(self):
        return self.job.host_id

    def get_extra_vars(self):
        """Hand the playbook what it needs to take the panel's access off.

        ``ic_panel_key`` is the public half of the key the panel logs in
        with (empty for a password login, or a key it cannot read), the
        line the playbook removes from every ``authorized_keys`` of the
        server.
        ``ic_operator_user`` is the account hardening creates when the
        panel connected as root, which the playbook removes only when the
        panel made it (``files/remove_panel_access.sh``).

        :return: the playbook's extra-vars
        :rtype: dict
        """
        host = self._host()
        return {
            "ic_panel_key": host._ssh_public_key() if host else "",
            "ic_operator_user": DEFAULT_HARDENED_USER,
        }

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
