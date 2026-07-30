"""Install the per-host observability agents (Fase 4 / A2).

Machine state on a host, so it is Ansible — the frontier drawn in Fase 3
("machine state = Ansible; application operation = versioned script")
applies unchanged.

What this executor owns that the playbook cannot: the **label map**. Only
the panel knows which compose project belongs to which instance, project
and tenant, so it generates that mapping and hands it over as extra-vars.
Labels are attached at ingest because they cannot be added to historical
series afterwards.
"""
import logging

from odoo import _
from odoo.exceptions import UserError

from .ansible_executor import AnsibleExecutor

_logger = logging.getLogger(__name__)


class ObservabilityAgentsExecutor(AnsibleExecutor):
    """Deploy node_exporter + cAdvisor + vmagent on this job's host."""

    _job_type = "install_observability"
    _playbook = "playbooks/host_observability.yml"

    def _host(self):
        """Return the host this job targets."""
        return self.job.host_id

    def _instance_labels(self, instance):
        """Return the label map for one instance.

        Core knows host, project and instance. ``tenant`` is deliberately
        empty here: core ships to partners and has no notion of tenants —
        the SaaS layer overrides this to fill it in. Keeping the key
        present (and empty) means the playbook template does not have to
        care which layer produced the map.
        """
        return {
            "compose_project": instance.doodba_project_name or instance.name,
            "instance_id": str(instance.id),
            "instance": instance.name or "",
            "project": (
                instance.project_id.name if instance.project_id else ""
            ),
            "tenant": "",
            "tenant_id": "",
            "dir": instance.get_remote_dir(),
        }

    def _instances(self):
        """Return the instances whose metrics this host should label.

        Draft instances are skipped: nothing of theirs runs on the host
        yet, so a relabel rule for them would never match anything.
        """
        return self._host().instance_ids.filtered(
            lambda i: i.state != "draft",
        )

    def get_extra_vars(self):
        """Hand the agent configuration and the label map to the playbook."""
        host = self._host()
        settings = self.env["cloud.settings"].sudo()._get_system()
        # The action is offered on every host page, so it can be pressed
        # before observability is configured. Fail here with something
        # actionable rather than deploying agents that push into the void
        # and look healthy while reporting nowhere.
        if not settings.metrics_enabled:
            raise UserError(_(
                "Observability is disabled. Enable it in Settings and set "
                "the remote-write URL before installing agents."
            ))
        if not (settings.metrics_remote_write_url or "").strip():
            raise UserError(_(
                "No remote-write URL is configured, so the agents would "
                "have nowhere to push. Set it in Settings first."
            ))
        instances = self._instances()
        self._sys(
            f"Installing observability agents on {host.name} "
            f"({len(instances)} instance(s) to label)."
        )
        return {
            "ic_host_id": str(host.id),
            "ic_host_name": host.name or "",
            "ic_remote_write_url": settings.metrics_remote_write_url or "",
            "ic_remote_write_token": (
                settings.metrics_remote_write_token or ""
            ),
            "ic_instances": [
                self._instance_labels(inst) for inst in instances
            ],
        }

    def parse_results(self, results):
        """Fail when the playbook did, and report what came up."""
        errors = []
        rc = results.get(self._playbook, {}).get("exit_status", 1)
        if rc != 0:
            errors.append(
                f"Observability agent install failed (rc={rc}). See the log."
            )
            return errors
        facts = self.playbook_facts()
        running = int(facts.get("ic_agents_running") or 0)
        if running < 3:
            errors.append(
                f"Only {running}/3 agent containers came up "
                f"(node-exporter, cadvisor, vmagent)."
            )
        return errors

    async def on_success(self, results):
        """Report the outcome and clear any previous failure alert."""
        facts = self.playbook_facts()
        self._sys(
            f"✓ Observability agents running. "
            f"{facts.get('ic_instances_labelled') or 0} instance(s) labelled."
        )
        self._resolve_alert("observability_agents_failed")

    async def on_failure(self, results, errors):
        """Alert so a silently unmonitored host does not go unnoticed."""
        for err in errors:
            self._sys(f"✗ {err}")
        self._alert(
            "observability_agents_failed",
            "Observability agents could not be installed on this host, so "
            "it reports no metrics. Re-run to retry.",
            level="warning",
        )
