"""Threshold rules evaluated against the metrics backend.

Design decision (ratified, P9-1): **no Alertmanager, no vmalert**. The
panel already owns a multichannel, audited alert pipeline
(``cloud.alert`` → bus/email/Telegram/webhook); adding a second alerting
engine would give the operator two places to look and two notions of
"alert". Instead a cron queries VictoriaMetrics over PromQL, compares
against a threshold, and raises/resolves ``cloud.alert`` through the
shared API.

A rule is data, not code: an operator adds or tunes one from the panel
without a release, and the tests instantiate rules directly.

**The staleness watchdog is a rule too** — an ``absent()`` expression
covers "this host stopped reporting", which is what replaces the SSH
probe's implicit liveness signal once the telemetry jobs retire.
"""
import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# The backend is local (or a short hop away) and the cron runs often;
# a slow backend must not pile cron workers up.
_QUERY_TIMEOUT = 10

# Alert raised when the metrics backend itself cannot be reached.
BACKEND_UNREACHABLE_CODE = "metrics_backend_unreachable"


def promql_query(
    base_url, expression, timeout=_QUERY_TIMEOUT, token=None, user=None,
):
    """Run a PromQL instant query and return ``[(labels, value), ...]``.

    Shared by every consumer of the metrics backend (the rule cron and the
    instance-liveness cron) so there is one place that knows the wire
    format. Raises on transport or protocol failure — callers must treat
    an exception as "unknown", never as "healthy".

    :param token: password half of this panel's metrics credential.
    :param user: user half — the panel's metrics account. It is not
        cosmetic: the central derives the label filter it forces on the
        query from whoever authenticated, so querying as the wrong user
        silently returns another account's series or none at all. It was
        a fixed literal before per-account credentials existed; callers
        that omit it send no credentials at all rather than guessing, so
        a misconfiguration fails loudly instead of reading nothing.
    """
    response = requests.get(
        f"{base_url.rstrip('/')}/api/v1/query",
        params={"query": expression},
        timeout=timeout,
        auth=(user, token) if (token and user) else None,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError(
            f"PromQL query failed: {payload.get('error') or 'unknown'}"
        )
    samples = []
    for item in payload.get("data", {}).get("result", []):
        value = item.get("value") or [None, None]
        try:
            samples.append((item.get("metric", {}), float(value[1])))
        except (TypeError, ValueError):
            continue
    return samples


class CloudMetricRule(models.Model):
    _name = "cloud.metric.rule"
    _description = "Metric Threshold Rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    code = fields.Char(
        required=True,
        help="Alert code raised by this rule. Also the dedup key, so two "
             "rules sharing a code would fight over the same alert.",
    )
    expression = fields.Char(
        required=True,
        help="PromQL evaluated against the metrics backend. It should "
             "return one sample per target; the label named in "
             "'Host label' maps each sample back to a cloud.host.",
    )
    comparator = fields.Selection(
        [("gt", ">"), ("lt", "<")],
        default="gt",
        required=True,
        help="How the returned value is compared with the threshold.",
    )
    threshold = fields.Float(
        default=0.0,
        help="Value the sample is compared against. For presence rules "
             "written with absent(), leave the default and use '>' — "
             "absent() returns 1 exactly when the series is missing.",
    )
    level = fields.Selection(
        [("warning", "Warning"), ("critical", "Critical")],
        default="warning",
        required=True,
    )
    message = fields.Char(
        required=True,
        help="Text shown in the alert. '%(host)s' and '%(value)s' are "
             "substituted.",
    )
    scope = fields.Selection(
        [("host", "Host"), ("instance", "Instance")],
        string="Scope",
        default="host",
        required=True,
        help="What each sample of this rule describes. Instance-scoped "
             "rules resolve through the instance label the agents attach "
             "and raise their alert against that instance, so it shows up "
             "on its page rather than buried in its host's.",
    )
    instance_label = fields.Char(
        string="Instance label",
        default="instance_id",
        help="Label carrying the cloud.instance id, for instance-scoped "
             "rules.",
    )
    host_label = fields.Char(
        default="host_id",
        help="Metric label carrying the cloud.host id. A sample whose "
             "label does not resolve to a host is skipped rather than "
             "raising an unattributable alert.",
    )

    _sql_constraints = [
        ("code_uniq", "unique (code)", "A metric rule code must be unique."),
    ]

    # ===============================
    # EVALUATION
    # ===============================

    @api.model
    def _cron_evaluate(self):
        """Evaluate every active rule and sync the resulting alerts.

        Fail-safe: when the backend cannot be reached we raise one alert
        and **return without resolving anything**. Resolving on silence
        would turn an outage of the metrics stack into a screen full of
        green — the worst possible failure mode for an alerting system,
        and the easy mistake to make here.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return
        base = (settings.metrics_central_url or "").rstrip("/")
        if not base:
            _logger.warning(
                "[metrics] observability is enabled but no backend URL is "
                "configured; skipping evaluation.",
            )
            return

        user, token = settings._metrics_auth()
        Alert = self.env["cloud.alert"].sudo()
        rules = self.sudo().search([])
        if not rules:
            return

        try:
            results = {
                rule.id: rule._query(base, token=token, user=user)
                for rule in rules
            }
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            _logger.warning("[metrics] backend unreachable: %s", exc)
            Alert.raise_alert(
                BACKEND_UNREACHABLE_CODE,
                "The metrics backend is unreachable, so infrastructure "
                "alerts are not being evaluated. Metric-based alerts "
                "shown here may be stale.",
                level="critical",
            )
            return

        Alert.resolve_alert(BACKEND_UNREACHABLE_CODE)
        for rule in rules:
            rule._sync_alerts(results[rule.id])

    def _query(self, base_url, token=None, user=None):
        """Run this rule's PromQL and return ``[(labels, value), ...]``.

        Raises on transport/HTTP problems so the caller can treat a dead
        backend as "unknown", never as "healthy".

        :param token: password half of this panel's metrics credential.
        :param user: its account — the central filters the result by it.
        """
        self.ensure_one()
        return promql_query(
            base_url, self.expression, token=token, user=user,
        )

    def _breached(self, value):
        """Return True when *value* violates this rule's threshold."""
        self.ensure_one()
        if self.comparator == "gt":
            return value > self.threshold
        return value < self.threshold

    def _sync_alerts(self, samples):
        """Raise alerts for breaching hosts and resolve the rest.

        Only hosts this rule actually reported on are touched: a host
        missing from the result set is not silently declared healthy —
        that case belongs to an ``absent()`` rule, which is explicit.
        """
        self.ensure_one()
        Alert = self.env["cloud.alert"].sudo()
        Host = self.env["cloud.host"].sudo()
        Instance = self.env["cloud.instance"].sudo()
        for labels, value in samples:
            if self.scope == "instance":
                target = self._resolve_instance(Instance, labels)
                if not target:
                    continue
                # An instance can be legitimately silent — a plan that
                # sleeps it on idle stops every container, so cAdvisor
                # stops reporting and "down" is exactly what a naive
                # rule would conclude. Core has no notion of sleeping,
                # so it asks the instance and the SaaS layer answers.
                if target._metric_alerts_suppressed():
                    Alert.resolve_alert(self.code, instance=target)
                    continue
                text = self.message % {
                    "host": target.name,
                    "value": ("%g" % value),
                }
                if self._breached(value):
                    Alert.raise_alert(
                        self.code, text, level=self.level, instance=target,
                    )
                else:
                    Alert.resolve_alert(self.code, instance=target)
                continue

            host = self._resolve_host(Host, labels)
            if not host:
                continue
            text = self.message % {
                "host": host.name,
                "value": ("%g" % value),
            }
            if self._breached(value):
                Alert.raise_alert(
                    self.code, text, level=self.level, host=host,
                )
            else:
                Alert.resolve_alert(self.code, host=host)

    def _resolve_instance(self, Instance, labels):
        """Map a sample's labels back to a ``cloud.instance``, or False."""
        self.ensure_one()
        raw = (labels or {}).get(self.instance_label or "instance_id")
        if not raw:
            return False
        try:
            instance = Instance.browse(int(raw))
        except (TypeError, ValueError):
            return False
        return instance if instance.exists() else False

    def _resolve_host(self, Host, labels):
        """Map a sample's labels back to a live ``cloud.host``, or False.

        Archived hosts are dropped as deliberately as unknown ones. A
        host taken out of the fleet keeps its series in the backend
        until retention expires, and ``metrics_host_absent`` measures
        exactly that silence — so a machine the operator destroyed on
        purpose went on alerting as if it had failed. Its silence is the
        expected outcome, not a fault, and that holds for every
        host-scoped rule, not just the watchdog: nothing measured on a
        decommissioned box is worth an alert.
        """
        self.ensure_one()
        raw = (labels or {}).get(self.host_label or "host_id")
        if not raw:
            return False
        try:
            host = Host.browse(int(raw))
        except (TypeError, ValueError):
            return False
        if not host.exists() or not host.active:
            return False
        return host
