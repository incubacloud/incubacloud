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
    (a container restart, an out-of-memory kill), drawn over the series;
``table``
    an instant query per role, one row per instance (the server's "who
    uses it" table);
``state``
    a range query whose steps with a sample are "reporting" and whose
    gaps are "silent", one row per host (the fleet's ingestion line).

A panel's roles share one PromQL call: each expression is labelled with
its role (``ic_role``) and joined with ``or``, so the cost of a board is
one query per panel whatever the number of lines in it.

Every series of an instance or a host selects by ``instance_id`` or
``host_id``, never by names: ids are unique within this panel's account,
and the central forces the account label on every query this panel's
credential makes. The fleet selects nothing: read with the account's
credential it is that account's hosts; read with the operator's, on the
central's ``/admin-r`` route, every account's, which is why its
aggregations keep ``ic_account`` beside ``host_id``.
"""
import configparser
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from operator import itemgetter

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .cloud_host_direct import _SILENT_AFTER
from .cloud_instance_metrics import _ONE_OFF_NAMES
from .cloud_metric_rule import (
    BACKEND_UNREACHABLE_CODE,
    central_busy,
    promql_query,
    promql_query_range,
)
from .deploy_instance_executor import ODOO_COMMAND_PROD, ODOO_COMMAND_TEST
from .metrics_diagnosis import diagnose_host, diagnose_instance

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

#: The containers of a host's instances, their one-off runs left out.
_HOST_INSTANCES = _HOST_SELECTOR + ', instance_id!="", name!~"%(one_off)s"'

#: The database collector's readings, kept only while its file is fresh:
#: node_exporter serves the last file it found, so a collector that
#: stopped would repeat its last reading for ever. Three minutes, as the
#: database rules read it.
_PG_FRESH = (
    " and on (host_id) (time() - node_textfile_mtime_seconds{"
    + _HOST_SELECTOR + ', file=~".*ic_pg.prom"} < 180)'
)

#: Block devices that are not a disk an instance writes to: loop mounts
#: of snaps, RAM disks, optical drives, compressed swap in memory.
_DISKS = 'device!~"loop.*|ram.*|sr.*|zram.*"'

#: Network interfaces that are not the server's own link: loopback and
#: Docker's bridges and container ends, whose traffic the server's link
#: already counts once.
_LINKS = 'device!~"lo|veth.*|docker.*|br-.*"'

#: Rows of the fleet's ingestion line at most; hosts with a silence in
#: the window come first (12-nov-2026: a screen's worth, and the hosts
#: that need a look are never the ones left out).
_MAX_STATE_ROWS = 20

#: Share of the time some task of a host waited for CPU, memory or disk
#: (the kernel's PSI "some", as a fraction) above which the fleet counts
#: the host as under pressure. The same fifth the I/O rule of the
#: instance board uses (plan of 9-nov-2026, section 3.2; 12-nov-2026).
FLEET_PRESSURE_SHARE = 0.2

#: Root disk use, in percent, above which the fleet counts a host as
#: full: the threshold of the ``metrics_disk_critical`` rule.
FLEET_DISK_PERCENT = 90

#: Memory use, in percent, above which the fleet counts a host as short:
#: the threshold of the ``metrics_memory_high`` rule.
FLEET_MEMORY_PERCENT = 92

#: The central's operator read route, beside the account read route
#: (``/r``) that ``metrics_central_url`` names. Read-only: the same
#: endpoints as the accounts' route, without the account filter.
OPERATOR_READ_PREFIX = "/admin-r"

#: The suffix of ``metrics_central_url`` on a central this panel deploys.
_ACCOUNT_READ_SUFFIX = "/r"


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
    "host": (
        {
            "key": "load", "kind": "series", "unit": None,
            "digits": 2, "legend": None,
            "needs": "host",
            "queries": (
                # Runnable tasks per core: above 1 they queue.
                _q("load1", "max(node_load1{" + _HOST_SELECTOR + "})"
                   " / count(node_cpu_seconds_total{" + _HOST_SELECTOR
                   + ', mode="idle"})', peak=True),
                _q("load15", "max(node_load15{" + _HOST_SELECTOR + "})"
                   " / count(node_cpu_seconds_total{" + _HOST_SELECTOR
                   + ', mode="idle"})', peak=True),
            ),
        },
        {
            "key": "pressure", "kind": "series", "unit": "percent",
            "digits": 1, "legend": None,
            "needs": "host",
            "queries": (
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
            "key": "memory", "kind": "series", "unit": "percent",
            "digits": 1, "legend": None,
            "needs": "host",
            "queries": (
                # In use as the kernel counts it: what is not available,
                # page cache it can drop counted as free.
                _q("used", "100 * (1 - max(node_memory_MemAvailable_bytes{"
                   + _HOST_SELECTOR + "}) / max(node_memory_MemTotal_bytes{"
                   + _HOST_SELECTOR + "}))", peak=True),
                # A server without swap has no line here.
                _q("swap", "100 * (1 - max(node_memory_SwapFree_bytes{"
                   + _HOST_SELECTOR + "}) / (max(node_memory_SwapTotal_bytes{"
                   + _HOST_SELECTOR + "}) > 0))", peak=True),
            ),
        },
        {
            "key": "cpu_instances", "kind": "series", "unit": "percent",
            "digits": 1, "legend": "instance_id", "others": True,
            "needs": "host",
            "queries": (
                _q("use", "100 * sum by (instance_id, instance) ("
                   "rate(container_cpu_usage_seconds_total{"
                   + _HOST_INSTANCES + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "memory_instances", "kind": "series", "unit": "bytes",
            "digits": 0, "legend": "instance_id", "others": True,
            "needs": "host",
            "queries": (
                _q("use", "sum by (instance_id, instance) ("
                   "container_memory_working_set_bytes{"
                   + _HOST_INSTANCES + "})", peak=True),
            ),
        },
        {
            "key": "root", "kind": "series", "unit": "percent",
            "digits": 1, "legend": None,
            "needs": "host",
            "queries": (
                _q("used", "100 - 100 * max(node_filesystem_avail_bytes{"
                   + _HOST_SELECTOR + ', mountpoint="/"})'
                   " / max(node_filesystem_size_bytes{"
                   + _HOST_SELECTOR + ', mountpoint="/"})', peak=True),
            ),
        },
        {
            "key": "io", "kind": "series", "unit": "percent",
            "digits": 1, "legend": "device",
            "needs": "host",
            "queries": (
                # Share of the time each disk had a request in flight.
                _q("use", "100 * max by (device) ("
                   "rate(node_disk_io_time_seconds_total{"
                   + _HOST_SELECTOR + ", " + _DISKS + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "io_bytes", "kind": "series", "unit": "bytes_rate",
            "digits": 0, "legend": None,
            "needs": "host",
            "queries": (
                _q("read", "sum(rate(node_disk_read_bytes_total{"
                   + _HOST_SELECTOR + ", " + _DISKS + "}[%(window)ds]))"),
                _q("write", "sum(rate(node_disk_written_bytes_total{"
                   + _HOST_SELECTOR + ", " + _DISKS + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "disk_instances", "kind": "series", "unit": "bytes",
            "digits": 0, "legend": "instance_id", "others": True,
            "needs": "host",
            "queries": (
                # The collector names the instance ``instance_name``.
                _q("directory", "max by (instance_id, instance_name) ("
                   "ic_instance_disk_bytes{" + _HOST_SELECTOR + "})",
                   peak=True),
            ),
        },
        {
            "key": "network", "kind": "series", "unit": "bytes_rate",
            "digits": 0, "legend": None,
            "needs": "host",
            "queries": (
                _q("received", "sum(rate(node_network_receive_bytes_total{"
                   + _HOST_SELECTOR + ", " + _LINKS + "}[%(window)ds]))"),
                _q("sent", "sum(rate(node_network_transmit_bytes_total{"
                   + _HOST_SELECTOR + ", " + _LINKS + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "requests", "kind": "series", "unit": "rps",
            "digits": 3, "legend": "instance_id", "others": True,
            "needs": "host",
            "queries": (
                _q("use", "sum by (instance_id, instance) (rate("
                   "traefik_service_requests_total{" + _HOST_SELECTOR
                   + ', instance_id!=""}[%(window)ds]))'),
            ),
        },
        {
            "key": "latency", "kind": "series", "unit": "seconds",
            "digits": 3, "legend": "instance_id",
            "needs": "host",
            "queries": (
                _q("p95", "histogram_quantile(0.95, sum by "
                   "(instance_id, instance, le) (rate("
                   "traefik_service_request_duration_seconds_bucket{"
                   + _HOST_SELECTOR + ', instance_id!=""}[%(window)ds])))'),
            ),
        },
        {
            "key": "host", "kind": "stat", "unit": None,
            "digits": 2, "legend": None,
            "needs": "host",
            "queries": (
                _q("cores", 'count(node_cpu_seconds_total{'
                   + _HOST_SELECTOR + ', mode="idle"})'),
                _q("memory_total", "max(node_memory_MemTotal_bytes{"
                   + _HOST_SELECTOR + "})"),
                _q("root_size", "max(node_filesystem_size_bytes{"
                   + _HOST_SELECTOR + ', mountpoint="/"})'),
                # Seconds since the agents' last sample arrived, read the
                # way the ``metrics_host_absent`` rule reads it: a series
                # gone stale still has its last sample within the hour.
                _q("ingest_age", "time() - max(last_over_time(timestamp("
                   'up{job="node", ' + _HOST_SELECTOR + "})[1h:1m]))"),
                # 1 when the agents' last install put the database
                # collector in, 0 when it could not; none when it is
                # switched off or the agents predate it.
                _q("pg_collector", "max(ic_pg_collector{"
                   + _HOST_SELECTOR + "})"),
            ),
        },
        {
            "key": "consumers", "kind": "table", "unit": None,
            "digits": 2, "legend": "instance_id",
            "needs": "host",
            "queries": (
                # The last five minutes, whatever the board's range: a
                # table answers "who uses it now".
                _q("cpu", "100 * sum by (instance_id, instance) ("
                   "rate(container_cpu_usage_seconds_total{"
                   + _HOST_INSTANCES + "}[5m]))"),
                _q("memory", "sum by (instance_id, instance) ("
                   "container_memory_working_set_bytes{"
                   + _HOST_INSTANCES + "})"),
                _q("disk", "max by (instance_id) (last_over_time("
                   "ic_instance_disk_bytes{" + _HOST_SELECTOR + "}[30m]))"),
                _q("rps", "sum by (instance_id) (rate("
                   "traefik_service_requests_total{" + _HOST_SELECTOR
                   + ', instance_id!=""}[5m]))'),
                # The disk collector measures the database every ten
                # minutes and the filestore every ten or every hour by the
                # server's memory: twice that covers a missed run.
                _q("database", "max by (instance_id) (last_over_time("
                   "ic_pg_db_size_bytes{" + _HOST_SELECTOR + "}[30m]))"),
                _q("filestore", "max by (instance_id) (last_over_time("
                   "ic_instance_filestore_bytes{" + _HOST_SELECTOR + "}[2h]))"),
                _q("connections", "max by (instance_id, host_id) ("
                   "ic_pg_connections{" + _HOST_SELECTOR + "})" + _PG_FRESH),
                _q("max_connections", "max by (instance_id, host_id) ("
                   "ic_pg_max_connections{" + _HOST_SELECTOR + "})" + _PG_FRESH),
                _q("waiting", "max by (instance_id, host_id) ("
                   "ic_pg_waiting_lock{" + _HOST_SELECTOR + "})" + _PG_FRESH),
            ),
        },
    ),
    "fleet": (
        {
            "key": "summary", "kind": "stat", "unit": None,
            "digits": 0, "legend": None,
            "needs": "fleet",
            "queries": (
                _q("hosts", "count(count by (ic_account, host_id) ("
                   'last_over_time(up{job="node"}[5m]))) or vector(0)'),
                _q("disk_full", "count(max by (ic_account, host_id) ("
                   '100 - 100 * node_filesystem_avail_bytes{mountpoint="/"}'
                   ' / node_filesystem_size_bytes{mountpoint="/"}) > '
                   + str(FLEET_DISK_PERCENT) + ") or vector(0)"),
                _q("memory_full", "count(max by (ic_account, host_id) ("
                   "100 - 100 * node_memory_MemAvailable_bytes"
                   " / node_memory_MemTotal_bytes) > "
                   + str(FLEET_MEMORY_PERCENT) + ") or vector(0)"),
                _q("pressure", "count(max by (ic_account, host_id) (rate("
                   '{__name__=~"node_pressure_(cpu|memory|io)_waiting_seconds_total"}'
                   "[5m])) > " + str(FLEET_PRESSURE_SHARE) + ") or vector(0)"),
                # The disk collector writes every ten minutes: half an
                # hour covers two missed runs.
                _q("deployed", "count(count by (ic_account, instance_id) ("
                   "last_over_time(ic_instance_disk_bytes[30m]))) or vector(0)"),
                _q("running", "count(count by (ic_account, instance_id) ("
                   'container_last_seen{instance_id!="", ' + SERVICE_LABEL
                   + '="odoo"})) or vector(0)'),
            ),
        },
        {
            "key": "load", "kind": "series", "unit": None,
            "digits": 2, "legend": "host",
            "needs": "fleet",
            "queries": (
                _q("use", "topk(10, max by (ic_account, host_id, host) ("
                   "node_load15) / on (ic_account, host_id) count by "
                   '(ic_account, host_id) (node_cpu_seconds_total{mode="idle"}))'),
            ),
        },
        {
            "key": "memory", "kind": "series", "unit": "percent",
            "digits": 1, "legend": "host",
            "needs": "fleet",
            "queries": (
                _q("use", "topk(10, max by (ic_account, host_id, host) ("
                   "100 - 100 * node_memory_MemAvailable_bytes"
                   " / node_memory_MemTotal_bytes))"),
            ),
        },
        {
            "key": "root", "kind": "series", "unit": "percent",
            "digits": 1, "legend": "host",
            "needs": "fleet",
            "queries": (
                _q("use", "topk(10, max by (ic_account, host_id, host) ("
                   '100 - 100 * node_filesystem_avail_bytes{mountpoint="/"}'
                   ' / node_filesystem_size_bytes{mountpoint="/"}))'),
            ),
        },
        {
            "key": "io", "kind": "series", "unit": "percent",
            "digits": 1, "legend": "host",
            "needs": "fleet",
            "queries": (
                _q("use", "topk(10, max by (ic_account, host_id, host) ("
                   "100 * rate(node_disk_io_time_seconds_total{" + _DISKS
                   + "}[%(window)ds])))"),
            ),
        },
        {
            "key": "ingest", "kind": "state", "unit": None,
            "digits": 0, "legend": "host",
            "needs": "fleet",
            "queries": (
                _q("samples", "max by (ic_account, host_id, host) ("
                   'count_over_time(up{job="node"}[%(window)ds]))'),
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
        return (
            labels.get("instance") or labels.get("instance_name")
            or labels.get("instance_id") or ""
        )
    if legend:
        return labels.get(legend) or ""
    return labels.get(_ROLE_LABEL) or ""


def _host_keys(panel, labels):
    """Return the host a fleet series is about, for its legend and link.

    :returns: ``{"host_id", "account"}`` on a panel whose legend is the
        host, else an empty dict: the other scopes' answers stay as
        they were.
    :rtype: dict
    """
    if panel.get("legend") != "host":
        return {}
    return {
        "host_id": labels.get("host_id") or "",
        "account": labels.get("ic_account") or "",
    }


def _others(dropped):
    """Add up the series a panel left out into one line, step by step.

    :param list dropped: shaped series, ``{"points": [[ts, value]]}``.
    :returns: one series with role ``others``, its points the sum of
        every dropped series at each moment one of them has.
    :rtype: dict
    """
    total = {}
    for series in dropped:
        for ts, value in series["points"]:
            total[ts] = total.get(ts, 0) + value
    return {
        "name": "", "role": "others", "id": "",
        "points": [[ts, total[ts]] for ts in sorted(total)],
    }


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
            **_host_keys(panel, labels),
            "points": [[int(ts), round(value, digits)] for ts, value in points],
        })
    others = None
    if len(out) > _MAX_SERIES:
        out.sort(
            key=lambda s: sum(p[1] for p in s["points"]) / len(s["points"]),
            reverse=True,
        )
        # A stacked panel adds the rest up into one line, so the stack
        # still reaches the server's total; the others just drop them.
        if panel.get("others"):
            others = _others(out[_MAX_SERIES - 1:])
            out = out[:_MAX_SERIES - 1]
        else:
            out = out[:_MAX_SERIES]
    # By name, each line before its limit: the legend then pairs them.
    out = sorted(out, key=lambda s: (s["name"], s["role"] == "limit", s["role"]))
    return out + [others] if others else out


def shape_table(panel, raw):
    """Turn an instant answer into one row per instance.

    :param dict panel: an entry of ``PANELS`` of kind ``table``.
    :param list raw: ``promql_query``'s answer, ``[(labels, value)]``.
    :returns: ``[{"id", "name", <role>: value, ...}]``, a role missing
        from a row when the central had nothing for it.
    :rtype: list[dict]
    """
    rows = {}
    for labels, value in raw:
        key = labels.get("instance_id") or ""
        if not key:
            continue
        row = rows.setdefault(key, {"id": key, "name": ""})
        row["name"] = row["name"] or _series_name(panel, labels)
        row[labels.get(_ROLE_LABEL) or ""] = round(value, panel["digits"])
    return list(rows.values())


def shape_state(panel, raw, start, end, step):
    """Turn a range answer into segments of reporting and silence.

    A step with a sample is ``ok``; a step without one is ``silent``;
    contiguous steps of one state make one segment. Hosts with a
    silence come first, then by name, ``_MAX_STATE_ROWS`` at most.

    :param dict panel: an entry of ``PANELS`` of kind ``state``.
    :param list raw: ``promql_query_range``'s answer.
    :param int start: window start, aligned to the step.
    :param int end: window end, aligned to the step.
    :param int step: seconds between two points.
    :returns: ``{"rows": [{"name", "host_id", "account", "segments":
        [{"start", "end", "state"}]}], "hidden": int}``.
    :rtype: dict
    """
    rows = []
    for labels, points in raw:
        seen = {int(ts) for ts, _value in points}
        segments = []
        for ts in range(start, end + 1, step):
            state = "ok" if ts in seen else "silent"
            if segments and segments[-1]["state"] == state:
                segments[-1]["end"] = ts + step
            else:
                segments.append({"start": ts, "end": ts + step, "state": state})
        if segments:
            segments[-1]["end"] = min(segments[-1]["end"], end)
        rows.append({
            "name": _series_name(panel, labels),
            **_host_keys(panel, labels),
            "segments": segments,
        })
    rows.sort(key=lambda r: (
        not any(seg["state"] == "silent" for seg in r["segments"]), r["name"],
    ))
    return {
        "rows": rows[:_MAX_STATE_ROWS],
        "hidden": max(0, len(rows) - _MAX_STATE_ROWS),
    }


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


#: What a panel that was not asked answers, by kind.
_EMPTY_ANSWERS = {
    "stat": {"value": {}},
    "markers": {"markers": []},
    "table": {"rows": []},
    "state": {"rows": [], "hidden": 0},
}


def run_board(scope, values, available, base, user, token,
              range_key=None, start=None, end=None, verify=True):
    """Run every panel of *scope* against the central, in parallel.

    A panel that fails does not take the board down: its entry carries
    ``error`` (``"refused"`` on a 401 or 403, ``"busy"`` on a 429,
    ``"unanswered"`` on anything else, for the caller to word) and the
    others are drawn. A
    panel whose record is missing (an instance placed on no host has no
    server to compare with) is answered empty without asking.

    :param str scope: a key of ``PANELS``.
    :param dict values: the placeholders' values, the ids among them.
    :param set available: which ``needs`` the record can satisfy
        (``instance``, ``host``, ``fleet``).
    :param str base: the central's read URL.
    :param str user: this panel's metrics account.
    :param str token: its password.
    :param verify: the central's certificate check
        (``cloud.settings._central_tls_verify``).
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
            if panel["kind"] in ("stat", "table"):
                raw = promql_query(
                    base, expression, timeout=_CHART_QUERY_TIMEOUT,
                    token=token, user=user, retries=0, verify=verify,
                )
                if panel["kind"] == "table":
                    return {"rows": shape_table(panel, raw)}
                return {"value": shape_stat(panel, raw)}
            raw = promql_query_range(
                base, expression, win_start, win_end, step,
                timeout=_CHART_QUERY_TIMEOUT, token=token, user=user,
                retries=0, verify=verify,
            )
            if panel["kind"] == "state":
                return shape_state(panel, raw, win_start, win_end, step)
            key = "markers" if panel["kind"] == "markers" else "series"
            return {key: shape_series(panel, raw, win_start, win_end)}
        except Exception as exc:  # noqa: BLE001 — one panel, reported
            _logger.warning(
                "[charts] %s panel %s failed: %s", scope, panel["key"], exc,
            )
            # Translated by the caller: ``_`` reads its language from the
            # calling frame, and this one runs in a thread of its own.
            if _refused(exc):
                return {"error": "refused"}
            return {"error": "busy" if central_busy(exc) else "unanswered"}

    panels = PANELS[scope]
    asked = [p for p in panels if p["needs"] in available]
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=_BOARD_WORKERS) as pool:
        futures = {p["key"]: pool.submit(run, p) for p in asked}
        answers = {key: future.result() for key, future in futures.items()}
    central_ms = (time.monotonic() - began) * 1000
    out = {}
    for panel in panels:
        empty = _EMPTY_ANSWERS.get(panel["kind"], {"series": []})
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


def metrics_source(env):
    """Return where this panel reads its charts, or why it cannot.

    The one place the boards decide that observability is off or has no
    central, so the instance, the host and the fleet say it alike.

    :param env: an Odoo environment (its language words the errors).
    :returns: ``{"ok": True, "base", "user", "token"}``, or ``{"ok":
        False, "reason": "disabled" | "unconfigured", "error"}``.
    :rtype: dict
    """
    settings = env["cloud.settings"].sudo()._get_system()
    if not settings.metrics_enabled:
        return {
            "ok": False, "reason": "disabled",
            "error": env._("Observability is not enabled."),
        }
    base = (settings.metrics_central_url or "").strip()
    if not base:
        return {
            "ok": False, "reason": "unconfigured",
            "error": env._("No metrics central is configured."),
        }
    user, token = settings._metrics_auth()
    return {
        "ok": True, "base": base, "user": user, "token": token,
        "verify": settings._central_tls_verify(),
    }


def operator_source(env, base):
    """Return the central's operator read route, when this panel has one.

    Only the panel that deployed the central holds the operator
    credential, and only a central it deployed has an account route
    ending in ``/r`` beside the operator's ``/admin-r``.

    :param env: an Odoo environment.
    :param str base: ``metrics_central_url``.
    :returns: ``{"base", "user", "token"}``, or None.
    :rtype: dict | None
    """
    user, token = env["cloud.settings"].sudo()._metrics_auth(operator=True)
    root = base.rstrip("/")
    if not token or not root.endswith(_ACCOUNT_READ_SUFFIX):
        return None
    return {
        "base": root[: -len(_ACCOUNT_READ_SUFFIX)] + OPERATOR_READ_PREFIX,
        "user": user, "token": token,
        "verify": env["cloud.settings"].sudo()._central_tls_verify(),
    }


def failing_since(env):
    """Return since when the central has failed, as its alert says.

    The rule cron raises ``metrics_backend_unreachable`` on its second
    failed evaluation in a row, a refused credential included: the
    alert's creation is the earliest failure the panel has confirmed.
    Before that there is none to give.

    :param env: an Odoo environment.
    :returns: ISO 8601 in UTC, or None while no such alert is open.
    :rtype: str | None
    """
    alert = env["cloud.alert"].sudo().search(
        [("code", "=", BACKEND_UNREACHABLE_CODE), ("state", "=", "active")],
        order="create_date", limit=1,
    )
    return _iso_utc(alert.create_date)


def _all_refused(board):
    """Whether the central refused every panel the board asked.

    :param dict board: ``run_board``'s answer, ``asked`` still in it.
    :rtype: bool
    """
    asked = board["asked"]
    return bool(asked) and all(
        board["panels"][key].get("error") == "refused" for key in asked
    )


def board_failure(env, board, user, token):
    """Say why a board has nothing, or word the panels that failed.

    Every panel failing is the central failing: a refused credential
    (401/403) is told apart from a central that does not answer, both
    with since when, and from a busy one (429 to every panel), which is
    not failing and has no since. Some panels failing leaves the others
    drawn, each failed one with its error in words.

    :param env: an Odoo environment.
    :param dict board: ``run_board``'s answer; ``asked`` is removed.
    :param str user: the credential's user half, to tell a refused one
        from one the panel does not have.
    :param str token: its password half.
    :returns: ``{"ok": False, "reason", "since", "error"}`` when every
        panel failed, else None.
    :rtype: dict | None
    """
    asked = board.pop("asked")
    failed = [key for key in asked if "error" in board["panels"][key]]
    if asked and len(failed) == len(asked):
        since = failing_since(env)
        if any(board["panels"][key]["error"] == "refused" for key in failed):
            return {
                "ok": False, "reason": "credential", "since": since,
                "error": (
                    env._("The metrics central refused this panel's credential.")
                    if user and token else
                    env._("This panel has no credential for the metrics central.")
                ),
            }
        if all(board["panels"][key]["error"] == "busy" for key in failed):
            return {
                "ok": False, "reason": "busy", "since": None,
                "error": env._(
                    "The metrics central is busy. The charts will ask again "
                    "shortly.",
                ),
            }
        return {
            "ok": False, "reason": "central", "since": since,
            "error": env._("The metrics central could not be reached."),
        }
    for key in failed:
        board["panels"][key]["error"] = (
            env._("The metrics central is busy.")
            if board["panels"][key]["error"] == "busy"
            else env._("The metrics central could not answer.")
        )
    return None


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
        source = metrics_source(self.env)
        if not source["ok"]:
            return source
        host = self.sudo().host_id
        values = {"instance_id": self.id, "host_id": host.id or 0}
        available = {"instance"} | ({"host"} if host else set())
        board = run_board(
            "instance", values, available,
            source["base"], source["user"], source["token"],
            range_key=range_key, start=start, end=end,
            verify=source.get("verify", True),
        )
        failure = board_failure(self.env, board, source["user"], source["token"])
        if failure:
            return failure
        workers = self._configured_workers()
        board["workers"] = {"configured": workers}
        board["diagnosis"] = diagnose_instance(
            board, self, workers, now=time.time(),
        )
        board["silence"] = self._metrics_silence()
        return {"ok": True, **board}

    def _metrics_failing_since(self):
        """Return since when the central has failed (see ``failing_since``).

        :returns: ISO 8601 in UTC, or None while no such alert is open.
        :rtype: str | None
        """
        return failing_since(self.env)

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
        silence = host._metrics_silence(now=now)
        if silence["code"] != "window":
            return silence
        if instance.state != "deployed" or not instance.metrics_last_seen:
            return {"code": "new", "since": None}
        if not instance.running:
            return {"code": "stopped", "since": None}
        return {"code": "window", "since": None}


class CloudHost(models.Model):
    _inherit = "cloud.host"

    def _metrics_board(self, range_key=None, start=None, end=None):
        """Read this server's board from the central and diagnose it.

        Every panel of ``PANELS["host"]`` with this panel's account
        credential, the "who uses it" table matched to the instances the
        caller can read, a plain-words diagnosis of the last 15 minutes
        and, for an empty board, why the central may have nothing.

        :param str range_key: one of ``CHART_RANGES``.
        :param start: zoom window start, unix seconds (optional).
        :param end: zoom window end, unix seconds (optional).
        :returns: ``{"ok": True, "start", "end", "step", "panels",
            "diagnosis", "silence", "timing"}``, or ``{"ok": False,
            "reason", "error"}`` as ``board_failure`` and
            ``metrics_source`` say.
        :rtype: dict
        """
        self.ensure_one()
        source = metrics_source(self.env)
        if not source["ok"]:
            return source
        board = run_board(
            "host", {"host_id": self.id}, {"host"},
            source["base"], source["user"], source["token"],
            range_key=range_key, start=start, end=end,
            verify=source.get("verify", True),
        )
        failure = board_failure(self.env, board, source["user"], source["token"])
        if failure:
            return failure
        consumers = board["panels"]["consumers"]
        consumers["rows"] = self._metrics_consumers(consumers.get("rows") or [])
        board["diagnosis"] = diagnose_host(board, self, now=time.time())
        board["silence"] = self._metrics_silence()
        return {"ok": True, **board}

    def _metrics_consumers(self, rows):
        """Match the central's rows of "who uses it" to the panel's instances.

        A row of an instance the caller can read gets its name, project
        and whether it runs, so the table links to its page; any other
        keeps the name the agents gave it and no link. Busiest first.

        :param list rows: ``shape_table``'s rows.
        :returns: the rows, each with ``project_id`` (False without a
            link), ``environment`` and ``running``.
        :rtype: list[dict]
        """
        ids = []
        for row in rows:
            try:
                ids.append(int(row["id"]))
            except (TypeError, ValueError):
                continue
        readable = {
            inst.id: inst
            for inst in self.env["cloud.instance"].search([("id", "in", ids)])
        }
        out = []
        for row in rows:
            inst = readable.get(int(row["id"])) if str(row["id"]).isdigit() else None
            out.append({
                **row,
                "name": inst.name if inst else row["name"],
                "project_id": inst.project_id.id if inst else False,
                "environment": inst.environment if inst else False,
                "running": bool(inst.running) if inst else None,
            })
        return sorted(
            out, key=lambda r: (-(r.get("cpu") or 0), -(r.get("memory") or 0)),
        )

    def _metrics_silence(self, now=None):
        """Say why the central may have nothing on this server.

        Codes: ``agents_failed``, ``agents_missing``, ``agents_waiting``
        (installed, nothing received yet), ``host_silent`` (quiet for
        ``_SILENT_AFTER``, ``since`` its last reading) and ``window``
        (it reports: the window is empty).

        :param datetime now: the current time, for tests.
        :returns: ``{"code": str, "since": str | None}``.
        :rtype: dict
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        host = self.sudo()
        if host.metrics_agents_state == "failed":
            return {"code": "agents_failed", "since": None}
        if host.metrics_agents_state != "installed":
            return {"code": "agents_missing", "since": None}
        if not host.metrics_last_seen:
            return {"code": "agents_waiting", "since": None}
        if host.metrics_last_seen < now - _SILENT_AFTER:
            return {"code": "host_silent", "since": _iso_utc(host.metrics_last_seen)}
        return {"code": "window", "since": None}

    @api.model
    def _fleet_metrics_board(self, range_key=None, start=None, end=None):
        """Read the fleet's board: every account's on the operator's panel.

        The panel that deployed the central reads it through the
        operator's read-only route and sees every account; any other
        panel, or a central that does not let this panel's address read
        that route yet, reads its own account's servers. ``scope`` says
        which, and each series and row says whether its host is one of
        this panel's (``own``), which is what may link to its page.

        :param str range_key: one of ``CHART_RANGES``.
        :param start: zoom window start, unix seconds (optional).
        :param end: zoom window end, unix seconds (optional).
        :returns: ``{"ok": True, "scope": "operator" | "account",
            "start", "end", "step", "panels", "alerts", "silence",
            "timing"}``, or ``{"ok": False, "reason", "error"}``.
        :rtype: dict
        """
        source = metrics_source(self.env)
        if not source["ok"]:
            return source
        board = None
        scope = "account"
        operator = operator_source(self.env, source["base"])
        if operator:
            board = run_board(
                "fleet", {}, {"fleet"},
                operator["base"], operator["user"], operator["token"],
                range_key=range_key, start=start, end=end,
                verify=operator.get("verify", True),
            )
            if _all_refused(board):
                # A central deployed before its operator route was open
                # to this panel's address: its own servers still show.
                _logger.info("[charts] fleet: operator route refused, "
                             "reading this panel's account instead")
                board = None
            else:
                scope = "operator"
                source = operator
        if board is None:
            board = run_board(
                "fleet", {}, {"fleet"},
                source["base"], source["user"], source["token"],
                range_key=range_key, start=start, end=end,
                verify=source.get("verify", True),
            )
        failure = board_failure(self.env, board, source["user"], source["token"])
        if failure:
            return failure
        account = self.env["cloud.settings"].sudo()._get_system().metrics_account
        for panel in board["panels"].values():
            for item in (panel.get("series") or []) + (panel.get("rows") or []):
                if "account" in item:
                    item["own"] = scope == "account" or item["account"] == account
                    if scope == "account":
                        item["account"] = ""
        board["scope"] = scope
        board["alerts"] = self._fleet_alert_counts()
        board["silence"] = self._fleet_silence()
        return {"ok": True, **board}

    @api.model
    def _fleet_alert_counts(self):
        """Count this panel's active alerts the caller can read, by kind.

        :returns: ``{"performance": int, "security": int}``; security is
            every code starting with ``security_``.
        :rtype: dict
        """
        Alert = self.env["cloud.alert"]
        active = [("state", "=", "active")]
        security = Alert.search_count(active + [("code", "=like", "security\\_%")])
        return {
            "performance": Alert.search_count(active) - security,
            "security": security,
        }

    @api.model
    def _fleet_silence(self):
        """Say why the fleet's board may be empty, from the panel's hosts.

        :returns: ``{"code": "no_hosts" | "agents_missing" | "window"}``:
            no server at all, none with the agents installed yet, or
            the window is empty.
        :rtype: dict
        """
        Host = self.sudo()
        if not Host.search_count([]):
            return {"code": "no_hosts"}
        if not Host.search_count([("metrics_agents_state", "=", "installed")]):
            return {"code": "agents_missing"}
        return {"code": "window"}
