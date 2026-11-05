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

from ._concurrency import read_committed_cursor
from .cloud_metric_rule import promql_query
from .host_metrics_executor import DISK_CRITICAL_THRESHOLD

_logger = logging.getLogger(__name__)

_GIB = 1024 ** 3

# One query per fact, keyed by the field it feeds.
# Each is the metric, its own label matchers, and what wraps the selector
# (``{}``); ``_query`` writes it, for some hosts only when asked.
_QUERIES = {
    "cpu_cores": ("node_cpu_seconds_total", 'mode="idle"', "count by (host_id) ({})"),
    "ram_total_gb": ("node_memory_MemTotal_bytes", "", "{}"),
    "disk_free_gb": ("node_filesystem_avail_bytes", 'mountpoint="/"', "{}"),
    "_disk_size": ("node_filesystem_size_bytes", 'mountpoint="/"', "{}"),
}


def _query(key, host_ids=None):
    """Return the PromQL expression of the fact *key*, for *host_ids* only.

    :param str key: a key of ``_QUERIES``
    :param host_ids: the hosts to ask about; every host when empty
    :rtype: str
    """
    metric, matchers, around = _QUERIES[key]
    parts = [matchers] if matchers else []
    if host_ids:
        ids = "|".join(str(int(host_id)) for host_id in sorted(host_ids))
        parts.append(f'host_id=~"{ids}"')
    selector = metric + ("{" + ",".join(parts) + "}" if parts else "")
    return around.format(selector)


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
        readings = self._read_specs_from_metrics()
        if readings is None:
            return

        now = fields.Datetime.now()
        # Every write of the pass happens here, on a READ COMMITTED
        # cursor of its own opened after the queries, and the cron's own
        # transaction writes nothing. These host rows are the ones the
        # durable hooks of ``host_metrics``, ``full_setup`` and
        # ``host_hardening`` and a save of the host write: under the
        # cron's snapshot, the second writer lost the row and the whole
        # pass rolled back, the disk status with it. A row written here
        # must never be written by the cron's transaction too, before or
        # after, or the two cursors would wait for each other.
        with read_committed_cursor(self.env.registry) as cr:
            env = self.env(cr=cr)
            # The central answered every query: what the convergence of
            # the direct path reads as "the central is up"
            # (cloud_host_direct).
            env["cloud.settings"].sudo()._direct_note_read_ok()
            env["cloud.host"]._write_specs_from_metrics(readings, now)

    @api.model
    def _read_specs_from_metrics(self, host_ids=None):
        """Ask the metrics backend what it knows of every host, or of some.

        :param host_ids: the hosts to ask about; every host when empty
        :return: ``{host_id: {field: value}}``, or ``None`` when there is
            nothing to read (metrics off, no central) or the backend did
            not answer; logged, never raised.
        :rtype: dict | None
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return None
        base = (settings.metrics_central_url or "").strip()
        if not base:
            return None
        user, token = settings._metrics_auth()

        readings = {}
        try:
            for key in _QUERIES:
                for labels, value in promql_query(
                    base, _query(key, host_ids), token=token, user=user,
                ):
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
            return None
        return readings

    def _refresh_specs_from_metrics_now(self):
        """Read these hosts' specs and disk from metrics now, not in 5 minutes.

        What ``_cron_refresh_specs_from_metrics`` does, for these hosts
        only (the backend is asked about them alone), when something just changed their disk or someone asked to
        see it: the host's Check, the end of a build or of a teardown.
        Written on the caller's cursor, inside a savepoint: the caller is
        a job's hook on its durable cursor, which may hold these rows
        already, and a second cursor would wait for it. A host with no
        metrics is left as it is, and nothing here ever raises: the
        disk shown is the cron's again in five minutes.
        """
        if not self:
            return
        try:
            readings = self.env["cloud.host"]._read_specs_from_metrics(self.ids)
            if not readings:
                return
            mine = {hid: readings[hid] for hid in self.ids if hid in readings}
            if not mine:
                return
            with self.env.cr.savepoint():
                self.env["cloud.host"]._write_specs_from_metrics(
                    mine, fields.Datetime.now(),
                )
        except Exception as exc:  # noqa: BLE001 — logged, never fatal
            _logger.warning(
                "[metrics] could not refresh the disk of host(s) %s: %s",
                self.ids, exc,
            )

    @api.model
    def _write_specs_from_metrics(self, readings, now):
        """Write what the metrics backend read of each host.

        Called on the specs cron's READ COMMITTED cursor: everything it
        writes goes through ``self.env``.

        :param dict readings: ``{host_id: {field: value}}`` as queried.
        :param datetime now: the time stamped as the hosts' last read.
        """
        for host in self.sudo().browse(list(readings)).exists():
            data = readings[host.id]
            # ``metrics_last_seen`` is written here and nowhere else. The
            # SSH fallback writes ``last_probed`` too, so anything that
            # asks "are metrics flowing?" must read this field instead —
            # otherwise the fallback answers its own question and stops
            # running exactly when it is needed.
            vals = {"last_probed": now, "metrics_last_seen": now}
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
