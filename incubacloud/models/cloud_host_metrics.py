"""Derive host specs and disk figures from metrics (Fase 4 / A9).

Replaces what ``host_metrics_executor`` collected over SSH — one job per
host every few minutes, on the ``root.bg`` channel that would not survive
a fleet of ~100 targets. node_exporter already reports all of it.

Scope note: this covers ``host_metrics`` **completely** (cpu_cores,
ram_total_gb, disk_usage, disk_free_gb, last_probed, and the
disk-critical alert, which is now a seeded ``cloud.metric.rule``), and
reads the live CPU and RAM usage the hosts' cards show, which the SSH
probe never did. It does
NOT cover ``instance_health``, which also does HTTP health probing and
error-log scraping — those need blackbox and Loki, both part of
observability v2 (a later batch of the metrics work, not the
roadmap's phase 2). That job therefore stays.
"""
import logging

from odoo import api, fields, models

from ._concurrency import read_committed_cursor
from .cloud_metric_rule import promql_query
from .host_metrics_executor import DISK_CRITICAL_THRESHOLD
from .host_probe_executor import (
    LOW_DISK_ALERT,
    LOW_DISK_CLEAR_GB,
    MIN_DISK_GB,
    MISSING_TOOLS_ALERT,
)

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
    # The busy share of all cores over the last five minutes: one cron
    # tick, so each reading covers the time since the one before.
    # ``iowait`` does not count as busy: the core is idle, waiting on the
    # disk, and a host that waits on its I/O would otherwise read as
    # loaded on a CPU bar that says how much compute is left (decided
    # 12-nov-2026). ``steal`` does count: it is compute the host's
    # provider took. The idle and iowait rates are added per host and
    # divided by its core count, since ``avg`` over both modes would halve
    # them.
    "cpu_usage": (
        "node_cpu_seconds_total", 'mode=~"idle|iowait"',
        "100 * (1 - sum by (host_id) (rate({}[5m])) / "
        'count by (host_id) (node_cpu_seconds_total{{mode="idle"}}))',
    ),
    "_mem_available": ("node_memory_MemAvailable_bytes", "", "{}"),
}

#: Seconds a CPU and RAM reading is shown for. Three ticks of the
#: five-minute specs cron, as ``HostMetricsExecutor._METRICS_FRESH_SECONDS``:
#: one missed tick must not blank the hosts' cards, and a host that stopped
#: reporting must not show its last figures as if they were current
#: (12-nov-2026).
USAGE_FRESH_SECONDS = 900


def _percent(value):
    """Return *value* as a percentage rounded to one decimal, within 0–100.

    :param float value: a percentage as computed, which a counter reset or
        a rounding can push a little out of range.
    :rtype: float
    """
    return round(min(100.0, max(0.0, value)), 1)


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
            # CPU and RAM go together or not at all: a host just enrolled
            # has no rate yet, and stamping half a reading would show the
            # other half's default 0 as a live figure.
            cpu = data.get("cpu_usage")
            available = data.get("_mem_available")
            total = data.get("ram_total_gb")
            if cpu is not None and available is not None and total:
                vals["cpu_usage"] = _percent(cpu)
                vals["ram_usage"] = _percent(100 - available / total * 100)
                vals["usage_last_seen"] = now
            host.write(vals)
            if "disk_usage" in vals:
                self._apply_disk_status(host, vals["disk_usage"])

    def _usage_for_card(self, metrics_enabled, now=None):
        """Return the live CPU and RAM usage a host's card shows, or why not.

        The figures are given only while ``usage_last_seen`` is recent
        (``USAGE_FRESH_SECONDS``); otherwise they are ``None`` and the
        state says why, so the card never shows a stale or default figure
        as current.

        :param bool metrics_enabled: whether metrics are on in Settings.
        :param datetime now: the time freshness is measured against.
        :return: ``usage_state`` (``live``, ``off`` with metrics off in
            Settings, ``no_agents`` with the agents not installed,
            ``agents_failed`` with their installation failed, or
            ``stale`` with no recent reading), ``cpu_usage`` and
            ``ram_usage`` in percent or ``None``, and ``usage_last_seen``
            as ISO text or ``''``.
        :rtype: dict
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        seen = self.usage_last_seen
        fresh = bool(seen) and (now - seen).total_seconds() <= USAGE_FRESH_SECONDS
        if not metrics_enabled:
            state = "off"
        elif fresh:
            state = "live"
        elif self.metrics_agents_state == "failed":
            state = "agents_failed"
        elif self.metrics_agents_state != "installed":
            state = "no_agents"
        else:
            state = "stale"
        live = state == "live"
        return {
            "usage_state": state,
            "cpu_usage": self.cpu_usage if live else None,
            "ram_usage": self.ram_usage if live else None,
            "usage_last_seen": seen.isoformat() if seen else "",
        }

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
            # A large disk may be this full and still have the room the
            # Check asks for: its low-disk alert must not wait for the
            # usage to drop.
            host._settle_check_disk_verdict()
            return
        self.env["cloud.alert"].sudo().resolve_alert(
            "disk_critical", host=host,
        )
        host._settle_check_disk_verdict()
        if host.status == "degraded" and not host._check_keeps_degraded():
            host.write({"status": "compatible"})

    def _settle_check_disk_verdict(self):
        """Resolve the Check's low-disk alert once the disk has room again.

        The Check (``host_probe``) raises ``LOW_DISK_ALERT`` and leaves the
        host ``degraded`` under ``MIN_DISK_GB`` free; the disk readings,
        every five minutes, are what notice the space was freed, so the
        verdict does not wait for the next Check. They close it only from
        ``LOW_DISK_CLEAR_GB`` (see there why one GB more), and never raise
        it: that stays the Check's word. One a user dismissed by hand is
        closed as resolved too, as ``_close_missing_tools_dismissed_by_hand``
        does for the tools. Reads ``disk_free_gb`` as just written by the
        caller.
        """
        self.ensure_one()
        if self.disk_free_gb < LOW_DISK_CLEAR_GB:
            return
        alerts = self.env["cloud.alert"].sudo()
        alerts.resolve_alert(LOW_DISK_ALERT, host=self)
        dismissed = alerts.search([
            ("code", "=", LOW_DISK_ALERT),
            ("host_id", "=", self.id),
            ("instance_id", "=", False),
            ("state", "=", "dismissed"),
            ("resolution", "=", "manual"),
        ])
        if dismissed:
            dismissed.write({"resolution": "auto"})

    def _check_keeps_degraded(self):
        """Whether the Check left this host degraded for a reason still open.

        A disk under ``DISK_CRITICAL_THRESHOLD`` is no reason to put back
        to ``compatible`` a host the Check found short of disk or of
        tools: the disk readings restore only what they degraded, and a
        small disk with no word of the Check is theirs (a disk over 95 %
        used they degraded, back at 8 GB free; a ``df`` that printed
        nothing, read as 0). Short of disk is the Check's low-disk alert:
        active, which closes only from ``LOW_DISK_CLEAR_GB``; or the
        newest one dismissed by hand while ``disk_free_gb``, just written
        by the caller, is still under ``MIN_DISK_GB`` -- dismissing it
        frees nothing. Short of tools is the newest
        ``MISSING_TOOLS_ALERT`` of the host: active, or dismissed by hand
        -- dismissing it installs nothing -- until a Check or a Full
        Setup that finds the tools closes it as resolved
        (``_close_missing_tools_dismissed_by_hand``).

        :rtype: bool
        """
        self.ensure_one()
        alerts = self.env["cloud.alert"].sudo()
        target = [("host_id", "=", self.id), ("instance_id", "=", False)]
        low_disk = alerts.search(
            [("code", "=", LOW_DISK_ALERT), *target],
            order="id desc", limit=1,
        )
        if low_disk.state == "active" or (
            low_disk.resolution == "manual"
            and self.disk_free_gb < MIN_DISK_GB
        ):
            return True
        newest = alerts.search(
            [("code", "=", MISSING_TOOLS_ALERT), *target],
            order="id desc", limit=1,
        )
        return bool(newest) and (
            newest.state == "active" or newest.resolution == "manual"
        )

    def _close_missing_tools_dismissed_by_hand(self):
        """Close as resolved the missing-tools alerts a user dismissed.

        For a Check or a Full Setup that found every tool in place: what
        a user had dismissed by hand is now resolved, and no longer keeps
        the host ``degraded`` (``_check_keeps_degraded``). The alert
        history then shows it as resolved, which it is.

        :return: the alerts closed.
        """
        self.ensure_one()
        dismissed = self.env["cloud.alert"].sudo().search([
            ("code", "=", MISSING_TOOLS_ALERT),
            ("host_id", "=", self.id),
            ("instance_id", "=", False),
            ("state", "=", "dismissed"),
            ("resolution", "=", "manual"),
        ])
        if dismissed:
            dismissed.write({"resolution": "auto"})
        return dismissed
