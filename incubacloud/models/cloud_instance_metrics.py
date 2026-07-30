"""Derive ``cloud.instance.running`` from metrics (Fase 4 / A8).

Today ``running`` is written by ``instance_health_executor`` — one SSH job
per instance every few minutes, the telemetry that does not scale past
~100 targets. This is its replacement: the same fact read from cAdvisor,
which the agents already report.

**Why cAdvisor and not Traefik.** ``running`` means *the stack exists and
is up*. "Is anyone using it" is a different fact, already modelled by
``sleeping`` (decided by Sablier from real traffic) and by
``last_activity_at``. If ``running`` were sourced from request traffic, a
healthy but idle instance would read as not-running, and the 14-day
auto-suspend that hangs off that chain would eventually act on legitimate
idleness — a billing consequence, not just a cosmetic one.

**Nothing is retired here.** The SSH telemetry keeps running until this
signal has been verified against real data; flipping the source and
deleting the old one in the same step is how you find out too late that
the new one had a blind spot.
"""
import logging

from odoo import api, models

from .cloud_metric_rule import promql_query

_logger = logging.getLogger(__name__)

# A container is considered up when cAdvisor saw it within this window.
# Comfortably above the 30 s scrape interval so one missed scrape does
# not flap the flag.
_SEEN_WINDOW_SECONDS = 180


class CloudInstance(models.Model):
    _inherit = "cloud.instance"

    @api.model
    def _cron_refresh_running_from_metrics(self):
        """Update ``running`` for instances the metrics backend covers.

        Fail-safe, twice over:

        * a backend that cannot be reached updates nothing — silence is
          not evidence that instances stopped; and
        * an instance the backend has never reported on is left untouched
          rather than marked stopped. Only instances with a known metrics
          footprint are flipped, so a host whose agents are not installed
          yet keeps whatever the SSH telemetry says.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return
        base = (settings.metrics_central_url or "").strip()
        if not base:
            return
        token = settings.metrics_remote_write_token or ""

        # One sample per instance: the most recent time any of its
        # containers was seen. ``instance_id`` is attached by the agent's
        # relabelling (see host_observability.yml).
        expression = (
            "max by (instance_id) (time() - container_last_seen{"
            'instance_id!=""})'
        )
        try:
            samples = promql_query(base, expression, token=token)
        except Exception as exc:  # noqa: BLE001 — logged, never fatal
            _logger.warning(
                "[metrics] could not refresh instance liveness: %s", exc,
            )
            return

        seen = {}
        for labels, age_seconds in samples:
            raw = (labels or {}).get("instance_id")
            if not raw:
                continue
            try:
                seen[int(raw)] = age_seconds
            except (TypeError, ValueError):
                continue
        if not seen:
            return

        instances = self.sudo().browse(list(seen)).exists()
        for inst in instances:
            running = seen[inst.id] <= _SEEN_WINDOW_SECONDS
            if inst.running != running:
                inst.write({"running": running})
                _logger.info(
                    "[metrics] instance %s running: %s → %s",
                    inst.name, not running, running,
                )

    @api.model
    def _metrics_liveness_window(self):
        """Return the freshness window, in seconds, for tests and docs."""
        return _SEEN_WINDOW_SECONDS
