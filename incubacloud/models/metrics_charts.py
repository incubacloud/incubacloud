"""The panel's own metric charts: windows, panels and one board per scope.

The browser never sends PromQL. It names a range, or a window inside the
last week when it zooms, and the expressions are filled in here from the
record's id and numbers computed on the server. Each scope (an instance
today; a host and the fleet build on the same pieces) is a list of
panels in ``PANELS``, plain data: a key, a kind, the queries it runs and
the unit the browser formats its values in.

Kinds:

``series``
    a range query per role, drawn as lines over time;
``stat``
    an instant query per role, one number each;
``markers``
    a range query per role whose points become moments on the time axis
    (a container restart, an out-of-memory kill), drawn over the series.

A panel's roles share one PromQL call: each expression is labelled with
its role (``ic_role``) and joined with ``or``, so the cost of a board is
one query per panel whatever the number of lines in it.

Every series selects by ``instance_id`` or ``host_id``, never by names:
ids are unique within this panel's account, and the central forces the
account label on every query this panel's credential makes.
"""
import configparser
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from operator import itemgetter

import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

from .cloud_host_direct import _SILENT_AFTER
from .cloud_instance_metrics import _ONE_OFF_NAMES
from .cloud_metric_rule import (
    BACKEND_UNREACHABLE_CODE,
    promql_query,
    promql_query_range,
)
from .deploy_instance_executor import ODOO_COMMAND_PROD, ODOO_COMMAND_TEST
from .metrics_diagnosis import diagnose_instance

_logger = logging.getLogger(__name__)

#: The ranges a board offers, in seconds. 6 h is where a board opens
#: when somebody asks "since when?"; 30 days is left out: retention
#: would allow it, but nobody asked and it is the dearest query.
CHART_RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}

#: At most this many points per series. More than a chart a few hundred
#: pixels wide can show, few enough to keep the answer small.
CHART_MAX_POINTS = 300

#: The agents' scrape interval (``host_observability.yml``): no step is
#: finer than this, as there is nothing in between to show.
_SCRAPE_SECONDS = 30

#: Narrowest window a zoom may ask for.
_MIN_WINDOW_SECONDS = 300

#: How far back a zoom may reach, a little over the widest range so a
#: zoom into the oldest part of "7d" still fits.
_MAX_LOOKBACK_SECONDS = 8 * 86400

#: Seconds the central has to answer each query, asked once: the page
#: waits on it, and a retry would double the wait of a central that is
#: already slow (10-nov-2026).
_CHART_QUERY_TIMEOUT = 8

#: Queries of one board in flight at once. A worker of the panel must
#: not hold a dozen connections to the central for one page.
_BOARD_WORKERS = 4

#: Lines a panel keeps at most; the rest are left out, by smallest mean.
_MAX_SERIES = 10

#: The compose service a container belongs to.
SERVICE_LABEL = "container_label_com_docker_compose_service"

#: The label each role's expression is tagged with in a joined query.
_ROLE_LABEL = "ic_role"

#: The containers of one instance, its one-off runs left out.
_INSTANCE_SELECTOR = 'instance_id="%(instance_id)d", name!~"%(one_off)s"'

#: The instance's Traefik series (the scrape relabels them by service).
_TRAEFIK_SELECTOR = 'instance_id="%(instance_id)d"'

#: A host's node_exporter series.
_HOST_SELECTOR = 'host_id="%(host_id)d"'


def _quantile(share):
    """Return the PromQL of a percentile of the instance's request time.

    :param str share: the quantile, ``"0.95"`` for p95.
    :rtype: str
    """
    return (
        f"histogram_quantile({share}, sum by (le) (rate("
        "traefik_service_request_duration_seconds_bucket{"
        + _TRAEFIK_SELECTOR + "}[%(window)ds])))"
    )


def _q(role, expr, peak=False):
    """Return one query of a panel.

    :param str role: what the query's lines are in the chart (``use``,
        ``limit``, ``p95``…); the browser draws by it.
    :param str expr: PromQL with ``%(…)d``/``%(…)s`` placeholders.
    :param bool peak: wrap in ``max_over_time`` when the step is wider
        than two scrapes, so a short peak of a gauge still shows on the
        7-day range. ``rate()`` does not need it: its window already
        covers the step.
    :rtype: dict
    """
    return {"role": role, "expr": expr, "peak": peak}


#: The panels of each scope, in the order the board shows them.
PANELS = {
    "instance": (
        {
            "key": "latency", "kind": "series", "unit": "seconds",
            "digits": 3, "legend": None,
            "needs": "instance",
            "queries": (
                _q("p50", _quantile("0.5")),
                _q("p95", _quantile("0.95")),
            ),
        },
        {
            "key": "requests", "kind": "series", "unit": "rps",
            "digits": 3, "legend": None,
            "needs": "instance",
            "queries": (
                _q("requests", "sum(rate(traefik_service_requests_total{"
                   + _TRAEFIK_SELECTOR + "}[%(window)ds]))"),
                _q("5xx", "sum(rate(traefik_service_requests_total{"
                   + _TRAEFIK_SELECTOR + ', code=~"5.."}[%(window)ds]))'),
            ),
        },
        {
            "key": "cpu", "kind": "series", "unit": "percent",
            "digits": 2, "legend": SERVICE_LABEL,
            "needs": "instance",
            "queries": (
                _q("use", "100 * sum by (" + SERVICE_LABEL + ") ("
                   "rate(container_cpu_usage_seconds_total{"
                   + _INSTANCE_SELECTOR + "}[%(window)ds]))"),
                # The quota Docker enforces (``cpus``), as a line.
                _q("limit", "100 * max by (" + SERVICE_LABEL + ") ("
                   "container_spec_cpu_quota{" + _INSTANCE_SELECTOR + "}"
                   " / container_spec_cpu_period{" + _INSTANCE_SELECTOR
                   + "})"),
            ),
        },
        {
            "key": "throttled", "kind": "series", "unit": "percent",
            "digits": 1, "legend": SERVICE_LABEL,
            "needs": "instance",
            "queries": (
                # Share of the scheduler's periods in which the container
                # wanted CPU and its quota said no.
                _q("use", "100 * sum by (" + SERVICE_LABEL + ") ("
                   "rate(container_cpu_cfs_throttled_periods_total{"
                   + _INSTANCE_SELECTOR + "}[%(window)ds]))"
                   " / sum by (" + SERVICE_LABEL + ") ("
                   "rate(container_cpu_cfs_periods_total{"
                   + _INSTANCE_SELECTOR + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "memory", "kind": "series", "unit": "bytes",
            "digits": 0, "legend": SERVICE_LABEL,
            "needs": "instance",
            "queries": (
                _q("use", "sum by (" + SERVICE_LABEL + ") ("
                   "container_memory_working_set_bytes{"
                   + _INSTANCE_SELECTOR + "})", peak=True),
                # A container without a limit reports 0: left out.
                _q("limit", "max by (" + SERVICE_LABEL + ") ("
                   "container_spec_memory_limit_bytes{"
                   + _INSTANCE_SELECTOR + "}) > 0"),
            ),
        },
        {
            "key": "events", "kind": "markers", "unit": None,
            "digits": 0, "legend": SERVICE_LABEL,
            "needs": "instance",
            "queries": (
                # The start time itself: each new value is a restart.
                _q("restart", "max by (" + SERVICE_LABEL + ") ("
                   "container_start_time_seconds{" + _INSTANCE_SELECTOR
                   + "})"),
                _q("oom", "sum by (" + SERVICE_LABEL + ") (increase("
                   "container_oom_events_total{" + _INSTANCE_SELECTOR
                   + "}[%(window)ds])) > 0"),
            ),
        },
        {
            "key": "pressure", "kind": "series", "unit": "percent",
            "digits": 1, "legend": None,
            "needs": "host",
            "queries": (
                # Share of time some task waited for the resource
                # (the kernel's PSI "some" line), on the whole server.
                _q("cpu", "100 * rate(node_pressure_cpu_waiting_seconds_total{"
                   + _HOST_SELECTOR + "}[%(window)ds])"),
                _q("memory", "100 * rate("
                   "node_pressure_memory_waiting_seconds_total{"
                   + _HOST_SELECTOR + "}[%(window)ds])"),
                _q("io", "100 * rate(node_pressure_io_waiting_seconds_total{"
                   + _HOST_SELECTOR + "}[%(window)ds])"),
            ),
        },
        {
            "key": "neighbours", "kind": "series", "unit": "percent",
            "digits": 1, "legend": "instance_id",
            "needs": "host",
            "queries": (
                _q("use", "100 * sum by (instance_id, instance) ("
                   "rate(container_cpu_usage_seconds_total{"
                   + _HOST_SELECTOR + ', instance_id!="", '
                   'name!~"%(one_off)s"}[%(window)ds]))'),
            ),
        },
        {
            "key": "disk", "kind": "series", "unit": "bytes",
            "digits": 0, "legend": None,
            "needs": "instance",
            "queries": (
                _q("directory", 'max(ic_instance_disk_bytes{'
                   'instance_id="%(instance_id)d"})', peak=True),
            ),
        },
        {
            "key": "host", "kind": "stat", "unit": None,
            "digits": 2, "legend": None,
            "needs": "host",
            "queries": (
                _q("cores", 'count(node_cpu_seconds_total{'
                   + _HOST_SELECTOR + ', mode="idle"})'),
                _q("load15", "max(node_load15{" + _HOST_SELECTOR + "})"),
            ),
        },
    ),
}


def chart_window(range_key=None, start=None, end=None, now=None):
    """Return ``(start, end, step)`` in unix seconds for a chart request.

    Either a named range ending now, or an explicit window (a zoom) that
    is clamped to the last ``_MAX_LOOKBACK_SECONDS`` and widened to at
    least ``_MIN_WINDOW_SECONDS``. The step is the smallest multiple of
    the scrape interval that keeps a series within ``CHART_MAX_POINTS``.

    :param str range_key: one of ``CHART_RANGES``; ignored when both
        ``start`` and ``end`` are given.
    :param start: window start, unix seconds.
    :param end: window end, unix seconds.
    :param now: current time, for tests.
    :rtype: tuple[int, int, int]
    :raises UserError: on an unknown range or a window that is not numbers.
    """
    now = int(now if now is not None else time.time())
    if start is not None and end is not None:
        try:
            start, end = int(start), int(end)
        except (TypeError, ValueError, OverflowError) as exc:
            raise UserError(_("The chart window is not valid.")) from exc
        end = min(end, now)
        start = max(start, now - _MAX_LOOKBACK_SECONDS)
        if end - start < _MIN_WINDOW_SECONDS:
            start = end - _MIN_WINDOW_SECONDS
    else:
        span = CHART_RANGES.get(range_key or "1h")
        if not span:
            raise UserError(_("Unknown chart range: %s", range_key))
        start, end = now - span, now
    raw_step = (end - start) / CHART_MAX_POINTS
    step = max(
        _SCRAPE_SECONDS,
        math.ceil(raw_step / _SCRAPE_SECONDS) * _SCRAPE_SECONDS,
    )
    # Align to the step so two refreshes a few seconds apart draw the
    # same points instead of shifting every one of them slightly.
    end = end - end % step
    start = start - start % step
    return start, end, step


def panel_expression(panel, values, step):
    """Return the one PromQL call that answers a whole panel.

    Each role's expression is filled from *values*, wrapped in
    ``max_over_time`` when it asks for peaks and the step is wider than
    two scrapes, tagged with its role and joined to the next with
    ``or``: the tags make the label sets distinct, so ``or`` keeps them
    all.

    :param dict panel: an entry of ``PANELS``.
    :param dict values: the placeholders' values.
    :param int step: the board's step, in seconds.
    :rtype: str
    """
    parts = []
    for query in panel["queries"]:
        expr = query["expr"] % values
        if query["peak"] and step > 2 * _SCRAPE_SECONDS:
            expr = f"max_over_time(({expr})[{step}s:{_SCRAPE_SECONDS}s])"
        parts.append(
            f'label_replace({expr}, "{_ROLE_LABEL}", "{query["role"]}", "", "")'
        )
    return " or ".join(parts)


def _series_name(panel, labels):
    """Return the name a series is shown under in a panel's legend."""
    legend = panel.get("legend")
    if legend == "instance_id":
        return labels.get("instance") or labels.get("instance_id") or ""
    if legend:
        return labels.get(legend) or ""
    return labels.get(_ROLE_LABEL) or ""


def shape_series(panel, raw, start, end):
    """Turn a range answer into what the browser draws for one panel.

    ``series`` kinds become ``[{name, role, points}]``, at most
    ``_MAX_SERIES`` lines (the largest by mean), ordered by name, a
    line before its limit.
    ``markers`` kinds become ``[{ts, role, name}]``: a restart at the
    container's new start time when it falls inside the window, an
    out-of-memory kill at the step where it was counted.

    :param dict panel: an entry of ``PANELS``.
    :param list raw: ``promql_query_range``'s answer.
    :param int start: window start.
    :param int end: window end.
    :rtype: list[dict]
    """
    digits = panel["digits"]
    if panel["kind"] == "markers":
        markers = []
        for labels, points in raw:
            role = labels.get(_ROLE_LABEL) or ""
            name = _series_name(panel, labels)
            if role == "restart":
                seen = {int(value) for _ts, value in points}
                markers.extend(
                    {"ts": ts, "role": role, "name": name}
                    for ts in sorted(seen) if start <= ts <= end
                )
            else:
                markers.extend(
                    {"ts": int(ts), "role": role, "name": name}
                    for ts, value in points if value > 0
                )
        return sorted(markers, key=itemgetter("ts"))
    out = []
    for labels, points in raw:
        if not points:
            continue
        out.append({
            "name": _series_name(panel, labels),
            "role": labels.get(_ROLE_LABEL) or "",
            "id": labels.get("instance_id") or "",
            "points": [[int(ts), round(value, digits)] for ts, value in points],
        })
    if len(out) > _MAX_SERIES:
        out.sort(
            key=lambda s: sum(p[1] for p in s["points"]) / len(s["points"]),
            reverse=True,
        )
        out = out[:_MAX_SERIES]
    # By name, each line before its limit: the legend then pairs them.
    return sorted(out, key=lambda s: (s["name"], s["role"] == "limit", s["role"]))


def shape_stat(panel, raw):
    """Turn an instant answer into ``{role: value}`` for a stat panel."""
    out = {}
    for labels, value in raw:
        out[labels.get(_ROLE_LABEL) or ""] = round(value, panel["digits"])
    return out


def _refused(exc):
    """Whether *exc* is the central turning this panel's credential down.

    A 401 or a 403: the central answered, so it is not down, and asking
    again with the same credential would answer the same.

    :param Exception exc: what a query of the board raised.
    :rtype: bool
    """
    if not isinstance(exc, requests.HTTPError):
        return False
    return getattr(exc.response, "status_code", None) in (401, 403)


def _iso_utc(moment):
    """Return a naive UTC datetime of the ORM as ISO 8601 with its offset.

    :param datetime moment: a ``fields.Datetime`` value, or False.
    :returns: ``"2026-11-12T09:30:00+00:00"``, or None without a moment.
    :rtype: str | None
    """
    if not moment:
        return None
    return moment.replace(tzinfo=timezone.utc).isoformat()


def run_board(scope, values, available, base, user, token,
              range_key=None, start=None, end=None):
    """Run every panel of *scope* against the central, in parallel.

    A panel that fails does not take the board down: its entry carries
    ``error`` (``"refused"`` on a 401 or 403, ``"unanswered"`` on
    anything else, for the caller to word) and the others are drawn. A
    panel whose record is missing (an instance placed on no host has no
    server to compare with) is answered empty without asking.

    :param str scope: a key of ``PANELS``.
    :param dict values: the placeholders' values, the ids among them.
    :param set available: which ``needs`` the record can satisfy
        (``instance``, ``host``).
    :param str base: the central's read URL.
    :param str user: this panel's metrics account.
    :param str token: its password.
    :returns: ``{"start", "end", "step", "panels": {key: {...}},
        "asked": [keys queried], "timing": {"central_ms"}}``.
    :rtype: dict
    """
    win_start, win_end, step = chart_window(range_key, start, end)
    values = values | {
        "one_off": _ONE_OFF_NAMES,
        "window": max(4 * _SCRAPE_SECONDS, step + _SCRAPE_SECONDS),
    }

    def run(panel):
        """Run one panel's query; never raises."""
        expression = panel_expression(panel, values, step)
        try:
            if panel["kind"] == "stat":
                raw = promql_query(
                    base, expression, timeout=_CHART_QUERY_TIMEOUT,
                    token=token, user=user, retries=0,
                )
                return {"value": shape_stat(panel, raw)}
            raw = promql_query_range(
                base, expression, win_start, win_end, step,
                timeout=_CHART_QUERY_TIMEOUT, token=token, user=user,
                retries=0,
            )
            key = "markers" if panel["kind"] == "markers" else "series"
            return {key: shape_series(panel, raw, win_start, win_end)}
        except Exception as exc:  # noqa: BLE001 — one panel, reported
            _logger.warning(
                "[charts] %s panel %s failed: %s", scope, panel["key"], exc,
            )
            # Translated by the caller: ``_`` reads its language from the
            # calling frame, and this one runs in a thread of its own.
            return {"error": "refused" if _refused(exc) else "unanswered"}

    panels = PANELS[scope]
    asked = [p for p in panels if p["needs"] in available]
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=_BOARD_WORKERS) as pool:
        futures = {p["key"]: pool.submit(run, p) for p in asked}
        answers = {key: future.result() for key, future in futures.items()}
    central_ms = (time.monotonic() - began) * 1000
    out = {}
    for panel in panels:
        empty = (
            {"value": {}} if panel["kind"] == "stat"
            else {"markers": []} if panel["kind"] == "markers"
            else {"series": []}
        )
        out[panel["key"]] = {
            "kind": panel["kind"],
            "unit": panel["unit"],
            **answers.get(panel["key"], empty),
        }
    return {
        "start": win_start,
        "end": win_end,
        "step": step,
        "panels": out,
        "asked": [p["key"] for p in asked],
        "timing": {"central_ms": round(central_ms, 1)},
    }


class CloudInstance(models.Model):
    _inherit = "cloud.instance"

    def _configured_workers(self):
        """Return how many HTTP workers this instance's Odoo runs.

        The command's ``--workers`` wins, as in Odoo: a staging's pins
        it (``ODOO_COMMAND_TEST``). Otherwise ``workers`` of its
        configuration; 0 (threaded mode) when it has none or one that
        is not a number.

        :rtype: int
        """
        self.ensure_one()
        command = (
            ODOO_COMMAND_PROD if self.environment == "production"
            else ODOO_COMMAND_TEST
        )
        for arg in command:
            if arg.startswith("--workers="):
                try:
                    return max(0, int(arg.split("=", 1)[1]))
                except ValueError:
                    return 0
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read_string(self.odoo_conf or "")
            return max(0, parser.getint("options", "workers", fallback=0))
        except (configparser.Error, ValueError):
            return 0

    def _metrics_board(self, range_key=None, start=None, end=None):
        """Read this instance's board from the central and diagnose it.

        :param str range_key: one of ``CHART_RANGES``.
        :param start: zoom window start, unix seconds (optional).
        :param end: zoom window end, unix seconds (optional).
        :returns: ``{"ok": True, "start", "end", "step", "panels",
            "diagnosis", "workers", "silence", "timing"}``, or ``{"ok":
            False, "reason", "error"}`` when observability is off
            (``disabled``) or has no central (``unconfigured``), plus
            ``since`` when the central refuses this panel's credential
            (``credential``) or does not answer (``central``).
        :rtype: dict
        """
        self.ensure_one()
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return {
                "ok": False, "reason": "disabled",
                "error": _("Observability is not enabled."),
            }
        base = (settings.metrics_central_url or "").strip()
        user, token = settings._metrics_auth()
        if not base:
            return {
                "ok": False, "reason": "unconfigured",
                "error": _("No metrics central is configured."),
            }
        host = self.sudo().host_id
        values = {"instance_id": self.id, "host_id": host.id or 0}
        available = {"instance"} | ({"host"} if host else set())
        board = run_board(
            "instance", values, available, base, user, token,
            range_key=range_key, start=start, end=end,
        )
        asked = board.pop("asked")
        failed = [key for key in asked if "error" in board["panels"][key]]
        if asked and len(failed) == len(asked):
            since = self._metrics_failing_since()
            if any(board["panels"][key]["error"] == "refused" for key in failed):
                return {
                    "ok": False, "reason": "credential", "since": since,
                    "error": (
                        _("The metrics central refused this panel's credential.")
                        if user and token else
                        _("This panel has no credential for the metrics central.")
                    ),
                }
            return {
                "ok": False, "reason": "central", "since": since,
                "error": _("The metrics central could not be reached."),
            }
        for key in failed:
            board["panels"][key]["error"] = _(
                "The metrics central could not answer.",
            )
        workers = self._configured_workers()
        board["workers"] = {"configured": workers}
        board["diagnosis"] = diagnose_instance(
            board, self, workers, now=time.time(),
        )
        board["silence"] = self._metrics_silence()
        return {"ok": True, **board}

    def _metrics_failing_since(self):
        """Return since when the central has failed, as its alert says.

        The rule cron raises ``metrics_backend_unreachable`` on its
        second failed evaluation in a row, a refused credential
        included: the alert's creation is the earliest failure the panel
        has confirmed. Before that there is none to give.

        :returns: ISO 8601 in UTC, or None while no such alert is open.
        :rtype: str | None
        """
        alert = self.env["cloud.alert"].sudo().search(
            [("code", "=", BACKEND_UNREACHABLE_CODE), ("state", "=", "active")],
            order="create_date", limit=1,
        )
        return _iso_utc(alert.create_date)

    def _metrics_silence(self, now=None):
        """Say why the central may have nothing on this instance.

        Read from what the panel keeps already, in the order a reader
        would look: the instance's server (are its agents there, has it
        sent anything, is it still sending), then the instance itself
        (deployed, ever reported, running). The board shows it only when
        no chart has a line to draw.

        Codes: ``new`` (no server, not deployed or never reported),
        ``agents_missing``, ``agents_failed``, ``agents_waiting``
        (installed, nothing received yet), ``host_silent`` (quiet for
        ``_SILENT_AFTER``, ``since`` its last reading), ``stopped`` and
        ``window`` (everything reports: the window is empty).

        :param datetime now: the current time, for tests.
        :returns: ``{"code": str, "since": str | None}``.
        :rtype: dict
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        instance = self.sudo()
        host = instance.host_id
        if not host:
            return {"code": "new", "since": None}
        if host.metrics_agents_state == "failed":
            return {"code": "agents_failed", "since": None}
        if host.metrics_agents_state != "installed":
            return {"code": "agents_missing", "since": None}
        if not host.metrics_last_seen:
            return {"code": "agents_waiting", "since": None}
        if host.metrics_last_seen < now - _SILENT_AFTER:
            return {
                "code": "host_silent",
                "since": _iso_utc(host.metrics_last_seen),
            }
        if instance.state != "deployed" or not instance.metrics_last_seen:
            return {"code": "new", "since": None}
        if not instance.running:
            return {"code": "stopped", "since": None}
        return {"code": "window", "since": None}
