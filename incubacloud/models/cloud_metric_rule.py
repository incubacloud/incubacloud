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

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# The backend is local (or a short hop away) and the cron runs often;
# a slow backend must not pile cron workers up.
_QUERY_TIMEOUT = 10

# Alert raised when the metrics backend itself cannot be reached.
BACKEND_UNREACHABLE_CODE = "metrics_backend_unreachable"


def promql_query(
    base_url, expression, timeout=_QUERY_TIMEOUT, token=None, user=None,
    retries=1,
):
    """Run a PromQL instant query and return ``[(labels, value), ...]``.

    Shared by every consumer of the metrics backend (the rule cron and the
    instance-liveness cron) so there is one place that knows the wire
    format. Raises on transport or protocol failure — callers must treat
    an exception as "unknown", never as "healthy".

    A transport failure (no connection, timeout) or a 5xx is asked once
    more on a fresh connection before it counts: a tenant panel reaches
    its central through a CDN, and on 2026-10-13 one edge delivered a
    request 9 s late while the next connection, through another edge,
    was answered in milliseconds. A 4xx or a PromQL error is not
    retried — it would answer the same twice, and retrying would only
    hide it.

    :param token: password half of this panel's metrics credential.
    :param user: user half — the panel's metrics account. It is not
        cosmetic: the central derives the label filter it forces on the
        query from whoever authenticated, so querying as the wrong user
        silently returns another account's series or none at all. It was
        a fixed literal before per-account credentials existed; callers
        that omit it send no credentials at all rather than guessing, so
        a misconfiguration fails loudly instead of reading nothing.
    :param int retries: extra attempts after a transport failure or 5xx.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}/api/v1/query",
                params={"query": expression},
                timeout=timeout,
                auth=(user, token) if (token and user) else None,
            )
            response.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            if attempt > retries:
                raise
            continue
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", 0) or 0
            if attempt > retries or status < 500:
                raise
            continue
        break
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
    confirm_cycles = fields.Integer(
        string="Confirm after",
        default=2,
        required=True,
        help="Evaluations in a row the threshold must be breached "
             "before the alert is raised. 1 raises on the first sample: "
             "right for an expression that already measures over a "
             "window, wrong for an instant reading, where a single "
             "spike would page someone.",
    )

    @api.constrains("confirm_cycles")
    def _check_confirm_cycles(self):
        """Refuse a cycle count that could never confirm."""
        for rule in self:
            if rule.confirm_cycles < 1:
                raise ValidationError(
                    _("A rule must confirm after at least one evaluation."),
                )

    # Partial on ``active``, not a plain UNIQUE (code): only active rules
    # are evaluated (``_cron_evaluate`` searches with the default
    # ``active_test``), so the damage this prevents exists only among
    # them — and a total UNIQUE would burn an archived rule's code
    # forever, a restriction nobody asked for. Same shape as
    # ``cloud_alert_active_target_uidx``.
    #
    # This was a ``_sql_constraints`` list until 1.0.98. Odoo 19 never
    # reads that attribute — ``_add_sql_constraints`` iterates
    # ``_table_objects``, which only descriptors like this one populate —
    # so the uniqueness silently did not exist, and the database
    # confirmed it: no unique index on ``code`` at all. The message is a
    # plain string on purpose: it is reflected into
    # ``ir.model.constraint.message`` (``translate=True``) and
    # translated from there, not through gettext at runtime.
    _code_uniq = models.UniqueIndex(
        "(code) WHERE active IS TRUE",
        "A metric rule code must be unique among active rules. The code "
        "is the alert's dedup key, so two active rules sharing one would "
        "fight over the same alert: whichever does not fire resolves the "
        "other's. Archive the old rule or give this one its own code.",
    )

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
            # Not on the first evaluation: ``cloud.alert.policy`` holds
            # how many in a row (two, shipped), and the retry inside
            # ``promql_query`` has already absorbed one hiccup by the
            # time we get here.
            Alert.raise_alert(
                BACKEND_UNREACHABLE_CODE,
                "The metrics backend is unreachable, so infrastructure "
                "alerts are not being evaluated. Metric-based alerts "
                "shown here may be stale.",
                level="critical",
                # A central never deployed from this panel is one it
                # reads but does not run: its outage is for whoever
                # runs it, not for this panel's operator.
                audience=(
                    "fleet" if settings.metrics_central_host_id
                    else "provider"
                ),
            )
            return

        Alert.resolve_alert(BACKEND_UNREACHABLE_CODE)
        for rule in rules:
            rule._sync_alerts(results[rule.id])

    @api.model
    def _confirm_window(self):
        """Seconds a sighting stays fresh: three ticks of this cron.

        Wide enough that a tick the runner delayed still counts as the
        next one; narrow enough that a breach seen this morning cannot
        confirm one seen tonight. Falls back to 15 minutes when the cron
        record is gone.
        """
        cron = self.env.ref(
            "incubacloud.cron_cloud_metric_rule_evaluate",
            raise_if_not_found=False,
        )
        if not cron:
            return 900
        unit = {
            "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800,
        }.get(cron.interval_type, 60)
        return 3 * max(cron.interval_number, 1) * unit

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
        confirm = {
            "confirm_after": self.confirm_cycles,
            "confirm_window": self._confirm_window(),
        }
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
                        **confirm,
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
                    self.code, text, level=self.level, host=host, **confirm,
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
