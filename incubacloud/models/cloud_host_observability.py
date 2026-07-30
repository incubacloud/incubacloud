"""Keep a host's metric label map in step with its instances.

vmagent's scrape config carries one relabelling block per instance on the
host — that is what turns a raw cAdvisor container sample into something
attributable to a ``cloud.instance``, a project and a tenant. The block
is rendered from the instance list at the moment the agents are
installed, so it is a snapshot.

Installing was originally chained from Host Setup only, which is the one
moment a host has no instances yet. Every instance deployed, moved or
removed afterwards left the snapshot stale: new instances reported
container metrics with no instance label at all (invisible on the
instance dashboard, unattributable in alerts), and removed ones kept a
label mapping pointing at nothing.

The playbook is idempotent, so the fix is simply to re-run it whenever
the set changes.
"""
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class CloudHost(models.Model):
    _inherit = "cloud.host"

    def refresh_observability_labels(self, reason=""):
        """Re-apply the metrics agents so the label map matches reality.

        Best-effort by design: this is called from the tail of operations
        that have already succeeded (a deploy, a move, a removal), and a
        monitoring refresh must never turn one of those into a failure.

        Only hosts already reporting are refreshed. A host that never had
        the agents installed is left alone — enrolling it here would be a
        surprise side effect of deploying an instance, and Host Setup is
        the place that decision belongs to.

        :param reason: short free text for the job log, e.g. 'deploy'.
        :return: the queued ``cloud.job`` id, or ``False`` if skipped.
        """
        self.ensure_one()
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return False
        if not (settings.metrics_remote_write_url or "").strip():
            return False
        if not self.last_probed:
            # Never seen by the metrics backend: the agents are not
            # installed here, and this is not the place to install them.
            return False
        try:
            return self.env["cloud.job"].sudo().enqueue(
                self.id, False, "install_observability",
                # Called from the on_success of a job that is still
                # 'started' on this host, so the active-job guard would
                # match that parent and refuse its own follow-up.
                bypass_running_check=True,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the caller
            _logger.warning(
                "[metrics] could not refresh labels on host %s (%s): %s",
                self.name, reason or "instance change", exc,
            )
            return False
