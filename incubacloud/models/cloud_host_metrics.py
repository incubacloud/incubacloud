"""Derive host specs and disk figures from metrics (Fase 4 / A9).

Replaces what ``host_metrics_executor`` collected over SSH — one job per
host every few minutes, on the ``root.bg`` channel that would not survive
a fleet of ~100 targets. node_exporter already reports all of it.

Scope note: this covers ``host_metrics`` **completely** (cpu_cores,
ram_total_gb, disk_usage, disk_free_gb, last_probed, and the
disk-critical alert, which is now a seeded ``cloud.metric.rule``). It does
NOT cover ``instance_health``, which also does HTTP health probing and
error-log scraping — those need blackbox and Loki, both part of
observability v2 (the deferred second batch of the metrics plan, not the
roadmap's phase 2). That job therefore stays.
"""
import logging

from odoo import api, fields, models

from .cloud_metric_rule import promql_query
from .host_metrics_executor import DISK_CRITICAL_THRESHOLD

_logger = logging.getLogger(__name__)

_GIB = 1024 ** 3

# One query per fact, keyed by the field it feeds.
_QUERIES = {
    "cpu_cores": 'count by (host_id) (node_cpu_seconds_total{mode="idle"})',
    "ram_total_gb": "node_memory_MemTotal_bytes",
    "disk_free_gb": 'node_filesystem_avail_bytes{mountpoint="/"}',
    "_disk_size": 'node_filesystem_size_bytes{mountpoint="/"}',
}


class CloudHost(models.Model):
    _inherit = "cloud.host"

    @api.model
    def _cron_refresh_specs_from_metrics(self):
        """Update specs/disk for hosts the metrics backend reports on.

        Same two fail-safes as the instance liveness cron: an unreachable
        backend updates nothing, and a host with no metrics is left alone
        rather than zeroed. Writing 0 cores / 0 GB free on silence would
        be worse than stale data — the auto-assign scoring reads these
        fields to place instances.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return
        base = (settings.metrics_central_url or "").strip()
        if not base:
            return
        token = settings.metrics_remote_write_token or ""

        readings = {}
        try:
            for key, expression in _QUERIES.items():
                for labels, value in promql_query(base, expression, token=token):
                    raw = (labels or {}).get("host_id")
                    if not raw:
                        continue
                    try:
                        host_id = int(raw)
                    except (TypeError, ValueError):
                        continue
                    readings.setdefault(host_id, {})[key] = value
        except Exception as exc:  # noqa: BLE001 — logged, never fatal
            _logger.warning(
                "[metrics] could not refresh host specs: %s", exc,
            )
            return

        if not readings:
            return

        now = fields.Datetime.now()
        for host in self.sudo().browse(list(readings)).exists():
            data = readings[host.id]
            vals = {"last_probed": now}
            if "cpu_cores" in data:
                vals["cpu_cores"] = int(data["cpu_cores"])
            if "ram_total_gb" in data:
                vals["ram_total_gb"] = round(data["ram_total_gb"] / _GIB, 1)
            free = data.get("disk_free_gb")
            size = data.get("_disk_size")
            if free is not None:
                vals["disk_free_gb"] = round(free / _GIB, 1)
            if free is not None and size:
                vals["disk_usage"] = round(100 - (free / size * 100), 1)
            host.write(vals)
            if "disk_usage" in vals:
                self._apply_disk_status(host, vals["disk_usage"])

    def _apply_disk_status(self, host, disk_usage):
        """Carry over the disk half of what the SSH probe used to do.

        The SSH executor did two things with a disk reading: raise the
        ``disk_critical`` alert, and flip ``status`` between
        ``compatible`` and ``degraded``. Alerting moved to a seeded
        ``cloud.metric.rule``, but the status flip had nowhere to go —
        and ``host_metrics`` now returns early whenever metrics are
        fresh, so once the handover happens nothing ever writes the
        status back. A host degraded by the last SSH probe before the
        cutover stays degraded forever, and ``select_best_host`` only
        places instances on ``compatible`` hosts: the host silently
        drops out of the pool for good.

        The legacy ``disk_critical`` alert is resolved here for the same
        reason — the rule raises its own code, so an alert left standing
        by the SSH path would never be cleared by anything.

        :param host: ``cloud.host`` record being refreshed.
        :param disk_usage: root-filesystem usage, percent.
        """
        if disk_usage >= DISK_CRITICAL_THRESHOLD:
            if host.status == "compatible":
                host.write({"status": "degraded"})
            return
        self.env["cloud.alert"].sudo().resolve_alert(
            "disk_critical", host=host,
        )
        if host.status == "degraded":
            host.write({"status": "compatible"})
