"""Deploy the central metrics stack (Fase 4 / A5).

VictoriaMetrics + Grafana, deployed by the panel like any other host
state — the owner's condition was "no manual steps, deployed the way
prepare-host is" (P9-5).

The target host is whichever ``cloud.host`` the job runs against, which
is what makes "co-locate now, move to a dedicated VPS later" a matter of
re-running the job elsewhere rather than a migration.
"""
import logging

from .ansible_executor import AnsibleExecutor

_logger = logging.getLogger(__name__)


class ObservabilityCentralExecutor(AnsibleExecutor):
    """Deploy VictoriaMetrics + Grafana on this job's host."""

    _job_type = "deploy_metrics_central"
    _playbook = "playbooks/observability_central.yml"

    def _host(self):
        """Return the host this job targets."""
        return self.job.host_id

    def get_extra_vars(self):
        """Hand retention and the shared write token to the playbook."""
        settings = self.env["cloud.settings"].sudo()._get_system()
        self._sys(
            f"Deploying the metrics central on {self._host().name} "
            f"(retention {settings.metrics_retention_days or 90} days)."
        )
        return {
            "ic_retention_days": settings.metrics_retention_days or 90,
            "ic_remote_write_token": (
                settings.metrics_remote_write_token or ""
            ),
        }

    def parse_results(self, results):
        """Require the backend to actually answer before calling it up."""
        errors = []
        rc = results.get(self._playbook, {}).get("exit_status", 1)
        if rc != 0:
            errors.append(
                f"Metrics central deployment failed (rc={rc}). See the log."
            )
            return errors
        if str(self.playbook_facts().get("ic_central_up", "")).lower() not in (
            "true", "1",
        ):
            errors.append(
                "VictoriaMetrics did not answer its health endpoint after "
                "deployment."
            )
        return errors

    async def on_success(self, results):
        """Report success and clear the failure alert."""
        self._sys(
            "✓ Metrics central is up. Point 'Metrics backend URL' at it "
            "and enable observability in Settings if you have not yet."
        )
        self._resolve_alert("metrics_central_failed")

    async def on_failure(self, results, errors):
        """Alert: without the central every metric alert is blind."""
        for err in errors:
            self._sys(f"✗ {err}")
        self._alert(
            "metrics_central_failed",
            "The metrics central could not be deployed. Metric-based "
            "alerts will not be evaluated until it is running.",
            level="critical",
        )
