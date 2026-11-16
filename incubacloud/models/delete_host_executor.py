"""
Delete Host Executor
--------------------
Stops the metrics agents and Traefik and removes their directories via
``ansible/playbooks/host_teardown.yml``, then takes the panel's access
off the server: its key, and the operator account and sudo grant that
hardening made for it. Then it archives the host record so it
disappears from the dashboard while preserving job history.

Queued for a host whose proxy was deployed, and for one the panel reached
without deploying it (``cloud.host._teardown_reaches_server``), which is
archived even when the teardown fails.

Prerequisites (enforced at the controller level):
  - Host must have zero active instances.
  - Host must not run the metrics central (also checked before the run,
    for a job queued before the central moved there).
"""

from .abstract_executor import CANCELLED_WHILE_RUNNING, JobRefused
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
        """Say what failed; archive a host that never had a proxy all the same.

        A host with its proxy deployed stays, so the teardown can run
        again. One without it reaches the teardown only because the panel
        had reached its server (``cloud.host._teardown_reaches_server``):
        before that it was archived with no teardown at all, and a server
        that is gone, or no longer lets the panel in, must not keep it from
        being deleted. Its record is archived, and the log says what may
        be left on the server.

        :param dict results: the playbook's result
        :param list errors: what failed
        """
        for err in errors:
            self._sys(f"✗ {err}")
        host = self._host()
        if host and not host.traefik_deployed:
            self._sys(
                f"⚠ Host '{host.name}' had no proxy deployed: its record is "
                "archived all the same. What the panel left on the server may "
                "still be there: as root on it, remove the panel's key from "
                "the authorized_keys of root and of the user the panel "
                "connected as, and /etc/sudoers.d/incubacloud-system."
            )
            host.write({'active': False})

    async def on_cancelled_running(self, results, exc):
        """A teardown a person stopped leaves the host as it is.

        ``on_failure`` would archive a host with no proxy deployed; a stop
        is not the deletion going ahead.

        :param results: what the steps answered, or None
        :param JobCancelled exc: the stop
        """
        self._sys(f"✗ {CANCELLED_WHILE_RUNNING} The host is kept.")
