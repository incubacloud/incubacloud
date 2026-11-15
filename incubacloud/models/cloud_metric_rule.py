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
import ipaddress
import logging
import re
import time

import requests

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# The backend is local (or a short hop away) and the cron runs often;
# a slow backend must not pile cron workers up.
_QUERY_TIMEOUT = 10

# Alert raised when the metrics backend itself cannot be reached.
BACKEND_UNREACHABLE_CODE = "metrics_backend_unreachable"

# Seconds to wait before asking again a central that answered 429.
_BUSY_PAUSE = 1

#: The rule whose alert names the addresses with the most attempts.
WEBLOGIN_CODE = "security_weblogin_bruteforce"

#: Minutes of lines those addresses are counted over: the rule's window.
WEBLOGIN_MINUTES = 10

#: Log reads one pass of the cron makes for those addresses at most, the
#: instances with the most attempts first; the rest go out without them
#: that pass (plan #84, C5, 14-nov-2026).
WEBLOGIN_LOOKUPS_PER_PASS = 10

#: Addresses an alert names.
WEBLOGIN_TOP = 3

#: What a rule's message may hold: its three markers and a literal ``%``.
#: Anything else with a ``%`` is a marker broken by a translation or an
#: edit, and the text falls back to English (review M6).
_MARKERS = re.compile(r"%%|%\((host|value|sources)\)s")


def central_busy(exc):
    """Whether *exc* is the central answering 429: busy, not down.

    vmauth answers 429 at once, without a queue, to a user over its cap
    on requests in flight (the operator's, 16) or over its own global
    limits. The central is up and answering, so callers do not count it
    as a failure: no ``metrics_backend_unreachable``, no chart saying
    the central cannot be reached.

    :param Exception exc: what a query raised.
    :rtype: bool
    """
    return (
        isinstance(exc, requests.HTTPError)
        and getattr(exc.response, "status_code", None) == 429
    )


def _promql_get(base_url, endpoint, params, timeout, token, user, retries,
                verify=True):
    """GET ``/api/v1/<endpoint>`` on the metrics backend and return its payload.

    The transport half shared by :func:`promql_query` and
    :func:`promql_query_range`: one retry policy, one way to
    authenticate, one check of the PromQL envelope. See
    :func:`promql_query` for why a transport failure, a 5xx or a 429 is
    asked once more and another 4xx is not.

    :param str base_url: metrics backend root.
    :param str endpoint: ``query`` or ``query_range``.
    :param dict params: query-string parameters.
    :param verify: as ``requests`` takes it; the panel passes
        ``cloud.settings._central_tls_verify()``, which trusts the
        central's own authority besides the usual roots.
    :returns: the decoded JSON payload, whose ``status`` is ``success``.
    :raises requests.RequestException: on transport or HTTP failure.
    :raises ValueError: when the backend answers with a PromQL error.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}/api/v1/{endpoint}",
                params=params,
                timeout=timeout,
                auth=(user, token) if (token and user) else None,
                verify=verify,
            )
            response.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            if attempt > retries:
                raise
            continue
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", 0) or 0
            if attempt > retries or (status < 500 and status != 429):
                raise
            if status == 429:
                time.sleep(_BUSY_PAUSE)
            continue
        break
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError(
            f"PromQL query failed: {payload.get('error') or 'unknown'}"
        )
    return payload


def promql_query_range(
    base_url, expression, start, end, step, timeout=_QUERY_TIMEOUT,
    token=None, user=None, retries=1, verify=True,
):
    """Run a PromQL range query and return ``[(labels, points), ...]``.

    ``points`` is a list of ``(unix seconds, float)`` in time order; a
    sample the backend sends as ``NaN`` or that does not parse is left
    out, so a gap in the data stays a gap. Same transport, retry and
    credential rules as :func:`promql_query`.

    :param str base_url: metrics backend root.
    :param str expression: PromQL to evaluate at every step.
    :param int start: first evaluation time, unix seconds.
    :param int end: last evaluation time, unix seconds.
    :param int step: seconds between evaluations.
    :param token: password half of this panel's metrics credential.
    :param user: user half — see :func:`promql_query`.
    :param int retries: extra attempts after a transport failure, a 5xx
        or a 429.
    :rtype: list[tuple[dict, list[tuple[float, float]]]]
    """
    payload = _promql_get(
        base_url, "query_range",
        {"query": expression, "start": int(start), "end": int(end),
         "step": int(step)},
        timeout=timeout, token=token, user=user, retries=retries,
        verify=verify,
    )
    series = []
    for item in payload.get("data", {}).get("result", []):
        points = []
        for raw_ts, raw_value in item.get("values") or []:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value != value:  # NaN
                continue
            points.append((float(raw_ts), value))
        series.append((item.get("metric", {}), points))
    return series


def promql_query(
    base_url, expression, timeout=_QUERY_TIMEOUT, token=None, user=None,
    retries=1, verify=True,
):
    """Run a PromQL instant query and return ``[(labels, value), ...]``.

    Shared by every consumer of the metrics backend (the rule cron and the
    instance-liveness cron) so there is one place that knows the wire
    format. Raises on transport or protocol failure — callers must treat
    an exception as "unknown", never as "healthy".

    A transport failure (no connection, timeout) or a 5xx is asked once
    more on a fresh connection before it counts: a panel may reach
    its central through a CDN, and on 2026-10-13 one edge delivered a
    request 9 s late while the next connection, through another edge,
    was answered in milliseconds. A 429 is asked again after
    ``_BUSY_PAUSE``: the central is busy, not wrong (see
    :func:`central_busy`). Another 4xx or a PromQL error is not retried —
    it would answer the same twice, and retrying would only hide it.

    :param token: password half of this panel's metrics credential.
    :param user: user half — the panel's metrics account. It is not
        cosmetic: the central derives the label filter it forces on the
        query from whoever authenticated, so querying as the wrong user
        silently returns another account's series or none at all. It was
        a fixed literal before per-account credentials existed; callers
        that omit it send no credentials at all rather than guessing, so
        a misconfiguration fails loudly instead of reading nothing.
    :param int retries: extra attempts after a transport failure, a 5xx
        or a 429.
    """
    payload = _promql_get(
        base_url, "query", {"query": expression},
        timeout=timeout, token=token, user=user, retries=retries,
        verify=verify,
    )
    samples = []
    for item in payload.get("data", {}).get("result", []):
        value = item.get("value") or [None, None]
        try:
            samples.append((item.get("metric", {}), float(value[1])))
        except (TypeError, ValueError):
            continue
    return samples


def top_sources(counted, limit=WEBLOGIN_TOP):
    """Return the *limit* sources with the most attempts, as an alert names them.

    Each address goes through ``ipaddress`` and comes out in its normal
    form; IPv6 is grouped by /64, since whoever holds one changes
    address at every attempt. What is not an address or a /64 network,
    or a count that is not a positive integer, is left out: what reaches
    an alert's text is never what a line said.

    :param counted: ``(address, count)`` pairs, or None.
    :param int limit: sources kept.
    :returns: ``(source, count)`` pairs, the most attempts first, a tie
        by source.
    :rtype: list[tuple[str, int]]
    """
    totals = {}
    for item in counted or ():
        try:
            raw, count = item
        except (TypeError, ValueError):
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            continue
        try:
            network = ipaddress.ip_network(str(raw), strict=True)
        except ValueError:
            continue
        mapped = (
            network.version == 6 and network.prefixlen == 128
            and network.network_address.ipv4_mapped
        )
        if mapped:
            # An IPv4 visitor on a dual-stack socket.
            network = ipaddress.ip_network(mapped)
        if network.version == 6:
            if network.prefixlen < 64:
                continue
            network = network.supernet(new_prefix=64)
            source = str(network)
        elif network.prefixlen == 32:
            source = str(network.network_address)
        else:
            continue
        totals[source] = totals.get(source, 0) + count
    ranked = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:limit]


class CloudMetricRule(models.Model):
    _name = "cloud.metric.rule"
    _description = "Metric Threshold Rule"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
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
        translate=True,
        help="Text shown in the alert, in the language the rules are "
             "evaluated in. '%(host)s' and '%(value)s' are substituted, "
             "and '%(sources)s' by what a rule adds (the web-login rule: "
             "the addresses with the most attempts); '%%' is a percent "
             "sign. A translation with any other marker falls back to the "
             "English text.",
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
    # ``cloud_alert_active_identity_uidx``.
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
        and the easy mistake to make here. A central that answers 429
        after the retry is busy, not down: the run returns without
        raising or resolving anything.
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
        verify = settings._central_tls_verify()
        Alert = self.env["cloud.alert"].sudo()
        rules = self.sudo().search([])
        if not rules:
            return

        try:
            results = {
                rule.id: rule._query(base, token=token, user=user, verify=verify)
                for rule in rules
            }
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            if central_busy(exc):
                # Up and answering, only full: neither an outage to
                # alert on nor an answer to resolve anything with. The
                # next run evaluates again.
                _logger.warning(
                    "[metrics] central busy (429); rules not evaluated "
                    "this run: %s", exc,
                )
                return
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
        # What a rule that reads more than its PromQL (the web-login
        # rule's log read) needs to reach the same central.
        central = {
            "base_url": base, "token": token, "user": user, "verify": verify,
        }
        for rule in rules:
            rule._sync_alerts(results[rule.id], central=central)

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

    def _query(self, base_url, token=None, user=None, verify=True):
        """Run this rule's PromQL and return ``[(labels, value), ...]``.

        Raises on transport/HTTP problems so the caller can treat a dead
        backend as "unknown", never as "healthy".

        :param token: password half of this panel's metrics credential.
        :param user: its account — the central filters the result by it.
        :param verify: the central's certificate check
            (``cloud.settings._central_tls_verify``).
        """
        self.ensure_one()
        return promql_query(
            base_url, self.expression, token=token, user=user, verify=verify,
        )

    def _breached(self, value):
        """Return True when *value* violates this rule's threshold."""
        self.ensure_one()
        if self.comparator == "gt":
            return value > self.threshold
        return value < self.threshold

    def _sync_alerts(self, samples, central=None):
        """Raise alerts for breaching hosts and resolve the rest.

        Only hosts this rule actually reported on are touched: a host
        missing from the result set is not silently declared healthy —
        that case belongs to an ``absent()`` rule, which is explicit.

        Each target is judged once, on its worst sample, and the targets
        are taken the furthest past the threshold first, so a rule that
        reads more for each breach within a budget a pass
        (:meth:`_message_extra`) spends it on the worst ones. The text is
        written in this environment's language (the cron's), and in
        English when the translation's markers are broken
        (:meth:`_render_message`).

        :param list samples: ``[(labels, value), ...]`` from :meth:`_query`.
        :param dict central: ``base_url``, ``token``, ``user`` and
            ``verify`` of the central the samples came from, for a rule
            that reads its logs too; None reads nothing more.
        """
        self.ensure_one()
        Alert = self.env["cloud.alert"].sudo()
        Host = self.env["cloud.host"].sudo()
        Instance = self.env["cloud.instance"].sudo()
        confirm = {
            "confirm_after": self.confirm_cycles,
            "confirm_window": self._confirm_window(),
        }
        # One verdict a target, on its worst sample: a server renamed
        # within a window answers twice for one host, and the second
        # sample would resolve what the first raised.
        worst = {}
        for labels, value in samples:
            if self.scope == "instance":
                target = self._resolve_instance(Instance, labels)
            else:
                target = self._resolve_host(Host, labels)
            if not target:
                continue
            rank = value if self.comparator == "gt" else -value
            if target.id not in worst or rank > worst[target.id][0]:
                worst[target.id] = (rank, target, labels, value)
        # What the hook has spent this pass (its log reads), the targets
        # the furthest past the threshold first.
        budget = {"lookups": 0}
        ordered = sorted(worst.values(), key=lambda item: -item[0])
        for _rank, target, labels, value in ordered:
            scope = (
                {"instance": target} if self.scope == "instance"
                else {"host": target}
            )
            # An instance can be legitimately silent — a layer that
            # sleeps it on idle stops every container, so cAdvisor
            # stops reporting and "down" is exactly what a naive rule
            # would conclude. Core has no notion of sleeping, so it
            # asks the instance and a layer that has it answers.
            if self.scope == "instance" and target._metric_alerts_suppressed():
                Alert.resolve_alert(self.code, **scope)
                continue
            # The panel restarts the server: silent, or measured as it
            # goes down and comes back, for a cut it asked for. Left as
            # it was, neither raised nor resolved (``_reboot_recent``).
            if self.scope != "instance" and target._reboot_recent():
                continue
            if not self._breached(value):
                Alert.resolve_alert(self.code, **scope)
                continue
            sources, payload = self._message_extra(
                target, labels, central, budget,
            )
            Alert.raise_alert(
                self.code, self._render_message(target.name, value, sources),
                level=self.level, payload=payload, **scope, **confirm,
            )

    def _render_message(self, name, value, sources=""):
        """Return this rule's message for one sample, in the env's language.

        The markers are filled by name, never with ``%``: a translation
        whose markers are broken (one the rule does not know, a ``%``
        left alone) is not used, and the English text is (review M6).
        An English text broken too, by an operator's edit, goes out with
        the markers it knows filled and the rest as written, logged: an
        alert with a stray marker beats a pass of the cron lost.

        :param str name: the host's or the instance's name, ``%(host)s``.
        :param float value: the sample, ``%(value)s``.
        :param str sources: ``%(sources)s``, what :meth:`_message_extra`
            added (empty for most rules).
        :rtype: str
        """
        self.ensure_one()
        values = {"host": name or "", "value": "%g" % value, "sources": sources}
        template = self.message or ""
        if not self._markers_ok(template):
            english = self.with_context(lang="en_US").message or ""
            if not self._markers_ok(english):
                _logger.warning(
                    "[metrics] rule %s: its message has a marker it does "
                    "not know; sent as written", self.code,
                )
            template = english
        return _MARKERS.sub(
            lambda match: "%" if match.group(0) == "%%" else values[match.group(1)],
            template,
        )

    @staticmethod
    def _markers_ok(template):
        """Whether every ``%`` of *template* is one of :data:`_MARKERS`.

        :param str template: a rule's message.
        :rtype: bool
        """
        return "%" not in _MARKERS.sub("", template)

    def _message_extra(self, target, labels, central, budget):
        """Return what this rule adds to a breach's alert: text and payload.

        The hook of the rules that say more than their figure; most say
        nothing more. Called for a breaching sample only, so nothing is
        read while no target is over its threshold.

        :param target: the ``cloud.host`` or ``cloud.instance`` alerted on.
        :param dict labels: the sample's labels.
        :param dict central: as :meth:`_sync_alerts` takes it, or None.
        :param dict budget: what this pass has spent, shared by the
            rule's samples; ``lookups`` counts its log reads.
        :returns: ``(sources, payload)``: the ``%(sources)s`` fragment,
            already in the env's language (``""`` for none), and the
            alert's payload (None leaves it alone).
        :rtype: tuple[str, dict | None]
        """
        self.ensure_one()
        if self.code == WEBLOGIN_CODE and self.scope == "instance":
            return self._weblogin_extra(target, labels, central, budget)
        return "", None

    def _weblogin_extra(self, instance, labels, central, budget):
        """The web-login rule's addresses: the most attempts, and how many.

        Read from the central's logs for this instance and the rule's
        window, at most :data:`WEBLOGIN_LOOKUPS_PER_PASS` reads a pass.
        A server reached through a CDN is not read: its proxy sees the
        CDN's addresses, not the visitors', and the text says so. No
        data (a central busy or failing, the budget spent) sends the
        alert without addresses. Nothing the attacker writes reaches the
        text: the name is the panel's, the addresses go through
        ``ipaddress`` (:func:`top_sources`), and the payload keeps
        addresses and counts, never a line.

        :param instance: the ``cloud.instance`` alerted on.
        :param dict labels: the sample's labels (``host_id``).
        :param dict central: as :meth:`_sync_alerts` takes it, or None.
        :param dict budget: this pass's spending, ``lookups``.
        :returns: ``(sources, payload)`` as :meth:`_message_extra`.
        :rtype: tuple[str, dict]
        """
        self.ensure_one()
        host = (
            self._resolve_host(self.env["cloud.host"].sudo(), labels)
            or instance.host_id
        )
        if host and host.behind_cdn:
            return (
                self.env._(
                    " Its server is behind a CDN: its proxy sees only the "
                    "CDN's addresses, so none is named.",
                ),
                {"sources": [], "behind_cdn": True},
            )
        top = []
        if central and host and budget["lookups"] < WEBLOGIN_LOOKUPS_PER_PASS:
            budget["lookups"] += 1
            try:
                counted = self._weblogin_top_sources(
                    host, instance.id, WEBLOGIN_MINUTES, central,
                )
            except Exception as exc:  # noqa: BLE001 - the alert goes out anyway
                # The reader answers None rather than raise; one that
                # raises still must not cost the pass its other alerts.
                _logger.warning(
                    "[metrics] web-login addresses of instance %s not "
                    "read: %s", instance.id, exc,
                )
                counted = None
            top = top_sources(counted)
        payload = {
            "sources": [{"address": a, "count": n} for a, n in top],
            "behind_cdn": False,
        }
        if not top:
            return "", payload
        listed = ", ".join(f"{address} ({count})" for address, count in top)
        return self.env._(" Most attempts: %(sources)s.", sources=listed), payload

    def _weblogin_top_sources(self, host, instance_id, minutes, central):
        """Return the failed web logins' addresses of an instance, counted.

        **The join with the log reader (plan #84, C2).** The reading is
        :func:`logs.weblogin_top_sources`: the last lines of
        ``{log_type="security", source="weblogin", host_id="…"} |=
        " instance=<id> "`` in the window, through the account's ``/lr``
        route (derived from ``central["base_url"]``) with the credential
        in *central*, never raising.

        :param host: the ``cloud.host`` whose proxy counted the attempts.
        :param int instance_id: the ``cloud.instance`` id.
        :param int minutes: the window, up to now.
        :param dict central: ``base_url``, ``token``, ``user``, ``verify``.
        :returns: ``(address, count)`` pairs, in any order and with IPv6
            addresses on their own or as /64 networks, or None when there
            is nothing to say (no lines, a central busy or failing).
        :rtype: list[tuple[str, int]] | None
        """
        # Imported here: logs.py takes its transport from this module.
        from . import logs

        return logs.weblogin_top_sources(
            central["base_url"], host.id, instance_id, minutes=minutes,
            token=central.get("token"), user=central.get("user"),
            verify=central.get("verify", True),
        )

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
