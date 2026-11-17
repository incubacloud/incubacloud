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
credential, as every board is, it is that account's hosts and nobody
else's. Its aggregations keep ``ic_account`` beside ``host_id``, which
is how the panel drops a series that is not of its account should a
central ever answer one (17-nov-2026: no panel reads another account's
servers, the one that deployed the central included).
"""
import configparser
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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

#: The rules whose thresholds the fleet's security figures count with,
#: so the board and the alerts agree, and the shipped threshold each
#: falls back to without its rule (plan #84, C7).
FLEET_SECURITY_RULES = {
    "ssh_threshold": ("security_ssh_bruteforce", 100),
    "weblogin_threshold": ("security_weblogin_bruteforce", 50),
}

#: The servers whose security logs do not reach the central, as the
#: ``security_logs_silent`` rule reads it, by account and server: the
#: collector silent for 15 minutes, its Loki output dropping lines, or
#: lines read in 30 minutes and none delivered. One series per server.
_FLEET_SECURITY_SILENT = (
    "count by (ic_account, host_id) ("
    '(max by (ic_account, host_id) (max_over_time(up{job="security"}[15m])) < 1)'
    " or (max by (ic_account, host_id) (increase("
    'fluentbit_output_dropped_records_total{name=~"loki.*"}[30m])) > 0)'
    " or ((sum by (ic_account, host_id) (increase("
    'ic_security_lines_total{source=~"sshd|sudo|weblogin"}[30m])) > 0)'
    " and on (ic_account, host_id) (max by (ic_account, host_id) (increase("
    'fluentbit_output_proc_records_total{name=~"loki.*"}[30m])) == 0)))'
)

#: The servers under SSH brute force now, and the instances under
#: web-login brute force now, over the rules' own windows.
_FLEET_SSH_BRUTEFORCE = (
    "round(sum by (ic_account, host_id) ("
    "increase(ic_ssh_auth_failed_total[10m]))) > %(ssh_threshold)s"
)
_FLEET_WEBLOGIN_BRUTEFORCE = (
    "round(sum by (ic_account, host_id, instance_id) (increase("
    'ic_weblogin_failed_total{instance_id!="0"}[10m]))) > %(weblogin_threshold)s'
)

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
    # A server's Security view (plan #84, C6): Fluent Bit's counters
    # alone, no address and no user in any of them. ``%(range)d`` is the
    # board's window in seconds, for the figures of the whole range.
    "host_security": (
        {
            "key": "ssh_failed", "kind": "series", "unit": None,
            "digits": 2, "legend": None,
            "needs": "host",
            "queries": (
                _q("failed", "60 * sum(rate(ic_ssh_auth_failed_total{"
                   + _HOST_SELECTOR + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "f2b_bans", "kind": "series", "unit": None,
            "digits": 2, "legend": "jail",
            "needs": "host",
            "queries": (
                _q("ban", "60 * sum by (jail) (rate(ic_f2b_total{"
                   + _HOST_SELECTOR + ', action="ban"}[%(window)ds]))'),
            ),
        },
        {
            "key": "accepted", "kind": "series", "unit": None,
            "digits": 2, "legend": "kind",
            "needs": "host",
            "queries": (
                _q("use", "3600 * sum by (kind) (rate(ic_ssh_accepted_total{"
                   + _HOST_SELECTOR + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "sudo", "kind": "series", "unit": None,
            "digits": 2, "legend": "kind",
            "needs": "host",
            "queries": (
                _q("use", "3600 * sum by (kind) (rate(ic_sudo_total{"
                   + _HOST_SELECTOR + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "audit", "kind": "series", "unit": None,
            "digits": 2, "legend": "who",
            "needs": "host",
            "queries": (
                _q("use", "3600 * sum by (who) (rate(ic_audit_events_total{"
                   + _HOST_SELECTOR + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "heartbeat", "kind": "series", "unit": None,
            "digits": 1, "legend": "source",
            "needs": "host",
            "queries": (
                _q("use", "3600 * sum by (source) (rate(ic_security_lines_total{"
                   + _HOST_SELECTOR + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "collector", "kind": "series", "unit": None,
            "digits": 1, "legend": None,
            "needs": "host",
            "queries": (
                # Lines the collector handed to the central and lines it
                # dropped, a minute; the security output is Loki's.
                _q("delivered", "60 * sum(rate(fluentbit_output_proc_records_total{"
                   + _HOST_SELECTOR + ', name=~"loki.*"}[%(window)ds]))'),
                _q("dropped", "60 * sum(rate(fluentbit_output_dropped_records_total{"
                   + _HOST_SELECTOR + ', name=~"loki.*"}[%(window)ds]))'),
            ),
        },
        {
            "key": "weblogin", "kind": "series", "unit": None,
            "digits": 2, "legend": "instance_id",
            "needs": "host",
            "queries": (
                # Named by the panel from the id (``_security_board``):
                # the counter carries the id alone.
                _q("use", "60 * sum by (instance_id) (rate(ic_weblogin_failed_total{"
                   + _HOST_SELECTOR + "}[%(window)ds]))"),
            ),
        },
        {
            "key": "http", "kind": "table", "unit": None,
            "digits": 3, "legend": "instance_id",
            "needs": "host",
            "queries": (
                # Means over the board's range, per second, and the
                # failed web logins of the range; ``instance_id="0"`` is
                # the attempts no instance was identified for.
                _q("rps", "sum by (instance_id) (increase("
                   "traefik_service_requests_total{" + _HOST_SELECTOR
                   + ', instance_id!=""}[%(range)ds])) / %(range)d'),
                _q("r4xx", "sum by (instance_id) (increase("
                   "traefik_service_requests_total{" + _HOST_SELECTOR
                   + ', instance_id!="", code=~"4.."}[%(range)ds])) / %(range)d'),
                _q("r5xx", "sum by (instance_id) (increase("
                   "traefik_service_requests_total{" + _HOST_SELECTOR
                   + ', instance_id!="", code=~"5.."}[%(range)ds])) / %(range)d'),
                _q("weblogin", "round(sum by (instance_id) (increase("
                   "ic_weblogin_failed_total{" + _HOST_SELECTOR
                   + "}[%(range)ds])))"),
            ),
        },
        {
            "key": "now", "kind": "stat", "unit": None,
            "digits": 0, "legend": None,
            "needs": "host",
            "queries": (
                # The SSH rule's window, so the board and the alert agree.
                _q("ssh_failed_10m", "round(sum(increase(ic_ssh_auth_failed_total{"
                   + _HOST_SELECTOR + "}[10m])))"),
                _q("bans_1h", "round(sum(increase(ic_f2b_total{"
                   + _HOST_SELECTOR + ', action="ban"}[1h])))'),
                _q("collector_up", 'max(last_over_time(up{job="security", '
                   + _HOST_SELECTOR + "}[5m]))"),
                _q("lines_15m", "round(sum(increase(ic_security_lines_total{"
                   + _HOST_SELECTOR + ', source=~"sshd|sudo|fail2ban|auditd"}[15m])))'),
                # The web-login jail's bans and lifts over the range: the
                # ban is of the whole server, not of one instance.
                _q("weblogin_bans", "round(sum(increase(ic_f2b_total{"
                   + _HOST_SELECTOR + ', jail="incubacloud-weblogin", '
                   'action="ban"}[%(range)ds])))'),
                _q("weblogin_unbans", "round(sum(increase(ic_f2b_total{"
                   + _HOST_SELECTOR + ', jail="incubacloud-weblogin", '
                   'action="unban"}[%(range)ds])))'),
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
                # ``group_left``: a division matched ``on`` some labels
                # keeps only those, and the server's name would be lost.
                _q("use", "topk(10, max by (ic_account, host_id, host) ("
                   "node_load15) / on (ic_account, host_id) group_left "
                   'count by (ic_account, host_id) ('
                   'node_cpu_seconds_total{mode="idle"}))'),
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
        # The servers' security (plan #84, C7): figures, never an
        # address or a user. Asked only where this panel's servers may
        # send their security logs (``fleet_security``).
        {
            "key": "security", "kind": "stat", "unit": None,
            "digits": 0, "legend": None,
            "needs": "fleet_security",
            "queries": (
                _q("ssh_failed_1h", "round(sum(sum by (ic_account, host_id) ("
                   "increase(ic_ssh_auth_failed_total[1h])))) or vector(0)"),
                _q("ssh_bruteforce", "count(" + _FLEET_SSH_BRUTEFORCE
                   + ") or vector(0)"),
                _q("bans_1h", "round(sum(sum by (ic_account, host_id) ("
                   'increase(ic_f2b_total{action="ban"}[1h])))) or vector(0)'),
                _q("weblogin_bruteforce", "count(" + _FLEET_WEBLOGIN_BRUTEFORCE
                   + ") or vector(0)"),
                _q("silent", "count(" + _FLEET_SECURITY_SILENT + ") or vector(0)"),
            ),
        },
        {
            # One row per server with something to look at, matched to
            # the panel's servers (``_fleet_security``).
            "key": "security_hosts", "kind": "hosts", "unit": None,
            "digits": 0, "legend": "host",
            "needs": "fleet_security",
            "queries": (
                _q("ssh_bruteforce", _FLEET_SSH_BRUTEFORCE),
                _q("weblogin_bruteforce", "count by (ic_account, host_id) ("
                   + _FLEET_WEBLOGIN_BRUTEFORCE + ")"),
                _q("silent", _FLEET_SECURITY_SILENT),
            ),
        },
        {
            "key": "security_ssh", "kind": "series", "unit": None,
            "digits": 0, "legend": None,
            "needs": "fleet_security",
            "queries": (
                _q("failed", "sum(sum by (ic_account, host_id) (rate("
                   "ic_ssh_auth_failed_total[%(window)ds]))) * 3600"),
                _q("bans", "sum(sum by (ic_account, host_id) (rate("
                   'ic_f2b_total{action="ban"}[%(window)ds]))) * 3600'),
            ),
        },
    ),
}

#: The panels of an instance's Metrics tab its summary by the hour reads
#: (``read_metrics`` of the MCP), the very entries of ``PANELS["instance"]``.
SUMMARY_PANELS = ("latency", "requests", "cpu", "memory")

#: The database collector's series of one instance, on its server.
_PG_INSTANCE = _HOST_SELECTOR + ', instance_id="%(instance_id)d"'

#: An instance's database connections over time, which the tab reads only
#: on demand from the server: the summary's one panel of its own. Kept
#: while the collector's file is fresh, as the server's table reads them.
_CONNECTIONS_PANEL = {
    "key": "connections", "kind": "series", "unit": None,
    "digits": 0, "legend": None,
    "needs": "host",
    "queries": (
        _q("connections", "max by (host_id) (ic_pg_connections{"
           + _PG_INSTANCE + "})" + _PG_FRESH, peak=True),
        _q("max_connections", "max by (host_id) (ic_pg_max_connections{"
           + _PG_INSTANCE + "})" + _PG_FRESH),
    ),
}

PANELS["instance_summary"] = tuple(
    panel for panel in PANELS["instance"] if panel["key"] in SUMMARY_PANELS
) + (_CONNECTIONS_PANEL,)

#: The widest window of a summary, in hours: the tab's widest range, 7 days.
SUMMARY_MAX_HOURS = CHART_RANGES["7d"] // 3600


def summary_step(board_step):
    """Return the step a summary by the hour reads its points at.

    The largest step that divides an hour (so every hour has the same
    number of points, none of them straddling two hours) and is a multiple
    of the scrape interval, without going over the board's own step:
    300 s for 24 hours, 1800 s for 168 (16-nov-2026: with the board's
    2040 s, an hour had one or two points and each was the mean of 34
    minutes, so an hour's ``max`` was no maximum).

    :param int board_step: the step ``chart_window`` gives the range.
    :rtype: int
    """
    return max(
        divisor for divisor in range(_SCRAPE_SECONDS, 3601, _SCRAPE_SECONDS)
        if 3600 % divisor == 0 and divisor <= max(board_step, _SCRAPE_SECONDS)
    )

#: A metric's highest point marks its hour as a peak when it is at least
#: this many times the window's average: below it, the line looks flat on
#: the tab's chart (15-nov-2026).
PEAK_FACTOR = 2

#: What the summary gives, in its order: the name, the panel and role it
#: reads (summed over the instance's services at each moment), the role of
#: its limit, and whether the limit is per service (``True``) or one.
_SUMMARY_METRICS = (
    ("cpu_percent", "cpu", "use", "limit", True),
    ("memory_bytes", "memory", "use", "limit", True),
    ("requests_per_s", "requests", "requests", None, False),
    ("errors_5xx_per_s", "requests", "5xx", None, False),
    ("p50_s", "latency", "p50", None, False),
    ("p95_s", "latency", "p95", None, False),
    ("db_connections", "connections", "connections", "max_connections", False),
)


def chart_window(range_key=None, start=None, end=None, now=None, step=None):
    """Return ``(start, end, step)`` in unix seconds for a chart request.

    Either a named range ending now, or an explicit window (a zoom) that
    is clamped to the last ``_MAX_LOOKBACK_SECONDS`` and widened to at
    least ``_MIN_WINDOW_SECONDS``. The step is the smallest multiple of
    the scrape interval that keeps a series within ``CHART_MAX_POINTS``,
    unless the caller asks for its own.

    :param str range_key: one of ``CHART_RANGES``; ignored when both
        ``start`` and ``end`` are given.
    :param start: window start, unix seconds.
    :param end: window end, unix seconds.
    :param now: current time, for tests.
    :param int step: the step to use, in seconds, instead of the computed one.
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
    if not step:
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


def shape_hosts(panel, raw):
    """Turn an instant answer into one row per account and server.

    :param dict panel: an entry of ``PANELS`` of kind ``hosts``.
    :param list raw: ``promql_query``'s answer, ``[(labels, value)]``.
    :returns: ``[{"host_id", "account", <role>: value, ...}]``, a role
        missing from a row when the server is not in its answer.
    :rtype: list[dict]
    """
    rows = {}
    for labels, value in raw:
        host_id = labels.get("host_id") or ""
        if not host_id:
            continue
        account = labels.get("ic_account") or ""
        row = rows.setdefault((account, host_id), {"host_id": host_id, "account": account})
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
    "hosts": {"rows": []},
    "state": {"rows": [], "hidden": 0},
}


def run_board(scope, values, available, base, user, token,
              range_key=None, start=None, end=None, verify=True, step=None):
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
        (``instance``, ``host``, ``fleet``, ``fleet_security``).
    :param str base: the central's read URL.
    :param str user: this panel's metrics account.
    :param str token: its password.
    :param verify: the central's certificate check
        (``cloud.settings._central_tls_verify``).
    :param int step: the step in seconds, instead of the window's own.
    :returns: ``{"start", "end", "step", "panels": {key: {...}},
        "asked": [keys queried], "timing": {"central_ms"}}``.
    :rtype: dict
    """
    win_start, win_end, step = chart_window(range_key, start, end, step=step)
    values = values | {
        "one_off": _ONE_OFF_NAMES,
        "window": max(4 * _SCRAPE_SECONDS, step + _SCRAPE_SECONDS),
    }

    def run(panel):
        """Run one panel's query; never raises."""
        expression = panel_expression(panel, values, step)
        try:
            if panel["kind"] in ("stat", "table", "hosts"):
                raw = promql_query(
                    base, expression, timeout=_CHART_QUERY_TIMEOUT,
                    token=token, user=user, retries=0, verify=verify,
                )
                if panel["kind"] == "table":
                    return {"rows": shape_table(panel, raw)}
                if panel["kind"] == "hosts":
                    return {"rows": shape_hosts(panel, raw)}
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


def _iso_ts(ts):
    """Return unix seconds as ISO 8601 in UTC, ``"2026-11-14T09:00:00+00:00"``."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _unix_ts(stamp):
    """Return an ISO 8601 time as unix seconds; one without a zone is UTC.

    :raises ValueError: when it is not a time.
    """
    moment = datetime.fromisoformat(str(stamp))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def _rounded(value, digits):
    """Round *value* to the panel's *digits*; an integer when it has none."""
    return int(round(value)) if digits == 0 else round(value, digits)


def summarize_hours(board):
    """Turn an ``instance_summary`` board into its summary by the hour.

    Each metric of ``_SUMMARY_METRICS`` is the sum of its role's lines at
    each moment (CPU and memory add up the instance's services). An hour
    (UTC) gives each metric's mean and highest point of the board's points
    in it; an hour with no point is left out, and the first and last may
    be partial. ``peaks`` gives each metric's highest point in the window
    above zero, how many times the window's average it is and its share of
    its limit; its hour lists it in ``peak`` when it is ``PEAK_FACTOR``
    times the average. With a limit per service (CPU, memory) the share is
    the largest of the services, each against its own limit
    (``limit_service`` names it): the sum of the services against the sum
    of their limits would hide one that is at its limit behind another at
    rest (16-nov-2026). A service without a limit is left out of it.

    :param dict board: ``run_board``'s answer, after ``board_failure``:
        a panel that failed carries its ``error`` in words.
    :returns: ``{"hours": [{"hour", <metric>: {"avg", "max"}, "peak"?}],
        "peaks": {metric: {"at", "hour", "value", "times_average",
        "share_of_limit"?, "limit_service"?}}, "limits": {metric: {service: value} |
        value}, "unanswered": {metric: error}}``.
    :rtype: dict
    """
    digits_of = {p["key"]: p["digits"] for p in PANELS["instance_summary"]}
    panels = board["panels"]
    hours, peaks, limits, unanswered = {}, {}, {}, {}
    for metric, key, role, limit_role, by_service in _SUMMARY_METRICS:
        panel = panels.get(key) or {}
        if panel.get("error"):
            unanswered[metric] = panel["error"]
            continue
        digits = digits_of[key]
        totals, own, caps = {}, {}, {}
        for series in panel.get("series") or []:
            if series["role"] == role:
                use = own.setdefault(series["name"], {})
                for ts, value in series["points"]:
                    totals[ts] = totals.get(ts, 0) + value
                    use[ts] = use.get(ts, 0) + value
            elif limit_role and series["role"] == limit_role and series["points"]:
                caps[series["name"]] = series["points"][-1][1]
        if caps:
            limits[metric] = (
                caps if by_service else _rounded(sum(caps.values()), digits)
            )
        for ts, value in totals.items():
            hours.setdefault(ts - ts % 3600, {}).setdefault(metric, []).append(value)
        if not totals:
            continue
        top_ts = min(totals, key=lambda ts: (-totals[ts], ts))
        top = totals[top_ts]
        if top <= 0:
            continue
        mean = sum(totals.values()) / len(totals)
        peak = {
            "at": _iso_ts(top_ts),
            "hour": _iso_ts(top_ts - top_ts % 3600),
            "value": _rounded(top, digits),
            "times_average": round(top / mean, 1),
        }
        if by_service:
            shares = [
                (max(use.values()) / caps[name], name)
                for name, use in own.items() if caps.get(name, 0) > 0
            ]
            if shares:
                share, service = max(shares, key=itemgetter(0, 1))
                peak["share_of_limit"] = round(100 * share, 1)
                peak["limit_service"] = service
        elif caps and sum(caps.values()) > 0:
            peak["share_of_limit"] = round(100 * top / sum(caps.values()), 1)
        peaks[metric] = peak
    rows = []
    for hour in sorted(hours):
        stamp = _iso_ts(hour)
        row = {"hour": stamp}
        for metric, key, *_rest in _SUMMARY_METRICS:
            values = hours[hour].get(metric)
            if values:
                row[metric] = {
                    "avg": _rounded(sum(values) / len(values), digits_of[key]),
                    "max": _rounded(max(values), digits_of[key]),
                }
        marked = [
            metric for metric, peak in peaks.items()
            if peak["hour"] == stamp and peak["times_average"] >= PEAK_FACTOR
        ]
        if marked:
            row["peak"] = marked
        rows.append(row)
    return {"hours": rows, "peaks": peaks, "limits": limits, "unanswered": unanswered}


class CloudInstance(models.Model):
    _inherit = "cloud.instance"

    def _metrics_label(self):
        """Return the name this instance has on a server's charts and table.

        ``project/instance``, as the job history names an instance: two
        projects may each have a ``prod``, and an instance named by a
        code is told apart by its project. Its own name without a
        project.

        :rtype: str
        """
        self.ensure_one()
        project = self.sudo().project_id.name
        return f"{project}/{self.name}" if project else (self.name or "")

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

    def _metrics_summary(self, hours=24, end=None):
        """Read this instance's metrics by the hour over the last *hours*.

        The panels of ``PANELS["instance_summary"]``, read from the
        central as the Metrics tab's board is (``run_board``), at a step
        that divides an hour (``summary_step``), and summed up by
        ``summarize_hours``.

        :param int hours: the window up to now, 1 to ``SUMMARY_MAX_HOURS``;
            outside it, the nearest of the two.
        :param str end: where the window ends, ISO 8601, as a previous
            answer gave it in ``end``, so that the pages of one reading
            share their window; now when omitted.
        :returns: ``{"ok": True, "window_hours", "start", "end", "step",
            "hours", "peaks", "limits", "unanswered", "silence"?,
            "timing"}``, or the board's ``{"ok": False, "reason",
            "error"}`` (``disabled``, ``unconfigured``, ``credential``,
            ``central``, ``busy``). ``silence`` is given only when no hour
            has a reading or the server or instance is not reporting.
        :rtype: dict
        :raises UserError: when *hours* or *end* is not valid.
        """
        self.ensure_one()
        try:
            hours = min(max(int(hours), 1), SUMMARY_MAX_HOURS)
            now = int(time.time())
            end = now if end in (None, "") else _unix_ts(end)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise UserError(_("The chart window is not valid.")) from exc
        end = min(end, now)
        source = metrics_source(self.env)
        if not source["ok"]:
            return source
        host = self.sudo().host_id
        values = {"instance_id": self.id, "host_id": host.id or 0}
        available = {"instance"} | ({"host"} if host else set())
        window = (end - hours * 3600, end)
        step = summary_step(chart_window(start=window[0], end=window[1])[2])
        board = run_board(
            "instance_summary", values, available,
            source["base"], source["user"], source["token"],
            start=window[0], end=window[1], step=step,
            verify=source.get("verify", True),
        )
        failure = board_failure(self.env, board, source["user"], source["token"])
        if failure:
            return failure
        summary = summarize_hours(board)
        if (
            "db_connections" not in summary["unanswered"]
            and not any("db_connections" in row for row in summary["hours"])
        ):
            summary["unanswered"]["db_connections"] = (
                _("The database collector has no reading in this window.")
                if host else _("The instance is on no server, so its database is not read.")
            )
        answer = {
            "ok": True,
            "window_hours": hours,
            "start": _iso_ts(board["start"]),
            "end": _iso_ts(board["end"]),
            "step": board["step"],
            **summary,
            "timing": board["timing"],
        }
        silence = self._metrics_silence()
        if not summary["hours"] or silence["code"] != "window":
            answer["silence"] = silence
        return answer

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
        self._metrics_name_instances(board["panels"])
        board["diagnosis"] = diagnose_host(board, self, now=time.time())
        board["silence"] = self._metrics_silence()
        return {"ok": True, **board}

    def _metrics_name_instances(self, panels):
        """Name the instances of a server's charts as the panel names them.

        The agents label a series with the instance's own name, which
        repeats from one project to the next and, where instances are
        named by a code, says nothing to whoever reads the legend. A
        series of an instance the caller can read is named with its
        project (``cloud.instance._metrics_label``), as the table of
        "who uses it" names it; any other keeps the agents' name. Each
        legend is put back in the order of its names, the line that adds
        up the rest last.

        :param dict panels: ``run_board``'s panels, changed in place.
        """
        series = [
            item for panel in panels.values() for item in panel.get("series") or []
            if str(item.get("id") or "").isdigit()
        ]
        ids = sorted({int(item["id"]) for item in series})
        labels = {
            inst.id: inst._metrics_label()
            for inst in self.env["cloud.instance"].search([("id", "in", ids)])
        }
        if not labels:
            return
        for item in series:
            item["name"] = labels.get(int(item["id"])) or item["name"]
        for panel in panels.values():
            if any(str(s.get("id") or "").isdigit() for s in panel.get("series") or []):
                panel["series"].sort(key=lambda s: (
                    s["role"] == "others", s["name"], s["role"] == "limit", s["role"],
                ))

    def _metrics_consumers(self, rows):
        """Match the central's rows of "who uses it" to the panel's instances.

        A row of an instance the caller can read gets its name with its
        project (``cloud.instance._metrics_label``), its project and
        whether it runs, so the table links to its page; any other keeps
        the name the agents gave it and no link. Busiest first.

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
                "name": inst._metrics_label() if inst else row["name"],
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
        """Read the fleet's board: this panel's own servers.

        Read with the panel's account credential, as an instance's and a
        server's boards are: the central filters that route by account,
        so the figures and the charts are of this panel's servers and of
        nobody else's. That holds for the panel that deployed the
        central too, which holds its operator credential: the board
        never reads with it (17-nov-2026; until then that panel drew
        every account's servers). Each series and row is then matched
        to this panel's servers (``_fleet_own_servers``).

        :param str range_key: one of ``CHART_RANGES``.
        :param start: zoom window start, unix seconds (optional).
        :param end: zoom window end, unix seconds (optional).
        :returns: ``{"ok": True, "start", "end", "step", "panels",
            "alerts", "silence", "security", "timing"}``, or ``{"ok":
            False, "reason", "error"}``.
        :rtype: dict
        """
        source = metrics_source(self.env)
        if not source["ok"]:
            return source
        security = self._security_logs_available()
        available = {"fleet", "fleet_security"} if security else {"fleet"}
        board = run_board(
            "fleet", self._fleet_security_thresholds(), available,
            source["base"], source["user"], source["token"],
            range_key=range_key, start=start, end=end,
            verify=source.get("verify", True),
        )
        failure = board_failure(self.env, board, source["user"], source["token"])
        if failure:
            return failure
        self._fleet_own_servers(board["panels"])
        board["alerts"] = self._fleet_alert_counts()
        board["silence"] = self._fleet_silence()
        board["security"] = self._fleet_security(board["panels"], security)
        return {"ok": True, **board}

    @api.model
    def _fleet_own_servers(self, panels):
        """Keep this panel's servers on the fleet's board, each by its name.

        The account credential already limits the answer to this panel's
        account. Should a central answer a series or a row of another
        account all the same (a hand-written gateway that does not
        filter), it is dropped here: another account's server is never
        named nor drawn. The account is then taken out of the answer,
        which has no use for it.

        A server the caller can read on this panel is named as the panel
        names it and marked ``own``, which is what may link to its
        board. Any other keeps the name its agents report, or
        "Server <id>" without one: no line of a legend is left unnamed.

        :param dict panels: ``run_board``'s panels, changed in place.
        """
        account = self.env["cloud.settings"].sudo()._get_system().metrics_account
        kept = []
        for panel in panels.values():
            for key in ("series", "rows"):
                items = panel.get(key)
                if not items or "account" not in items[0]:
                    continue
                panel[key] = [
                    item for item in items
                    if not (account and item["account"] and item["account"] != account)
                ]
                kept += panel[key]
        ids = {int(i["host_id"]) for i in kept if str(i["host_id"]).isdigit()}
        names = {host.id: host.name for host in self.search([("id", "in", sorted(ids))])}
        for item in kept:
            del item["account"]
            host_id = int(item["host_id"]) if str(item["host_id"]).isdigit() else 0
            item["own"] = host_id in names
            if "name" in item:
                item["name"] = (
                    names.get(host_id) or item["name"]
                    or self.env._("Server %s", item["host_id"] or "?")
                )
        # A legend reads in the order of the names it shows.
        for panel in panels.values():
            if panel.get("series") and "own" in panel["series"][0]:
                panel["series"].sort(key=itemgetter("name"))

    @api.model
    def _fleet_security_thresholds(self):
        """Return the thresholds the fleet's security figures count with.

        Those of the security rules, so a server the board counts as
        under brute force is one its rule alerts on; the shipped value
        where the rule is gone (``FLEET_SECURITY_RULES``).

        :returns: ``{"ssh_threshold": float, "weblogin_threshold": float}``.
        :rtype: dict
        """
        Rule = self.env["cloud.metric.rule"].sudo().with_context(active_test=False)
        out = {}
        for key, (code, default) in FLEET_SECURITY_RULES.items():
            rule = Rule.search([("code", "=", code)], limit=1)
            out[key] = float(rule.threshold if rule else default)
        return out

    @api.model
    def _fleet_security(self, panels, available):
        """Build the fleet's Security block: figures and this panel's servers.

        The central's figures (``panels["security"]``) stay in
        ``panels``; the servers under brute force or whose logs do not
        arrive come from ``panels["security_hosts"]``, which is taken
        out of the answer and kept only for the servers the caller can
        read on this panel (``own``, see ``_fleet_own_servers``). Which
        servers have their security logs off or no hardening, and which
        have active security alerts the caller can read, come from the
        panel's database. Never an address, a user or an alert's text.

        :param dict panels: ``run_board``'s panels, ``own`` marked.
        :param bool available: whether this panel's servers may send
            their security logs; without it the block is ``unavailable``
            and nothing of it was asked.
        :returns: ``{"state": "unavailable"}``, or ``{"state": "ready",
            "off", "unhardened", "alerts_hosts", "rows": [...],
            "hidden"}``.
        :rtype: dict
        """
        central = panels.pop("security_hosts", None) or {}
        if not available:
            return {"state": "unavailable"}
        flags = self.search([]).sudo()
        rows = {
            host.id: {
                "host_id": host.id, "name": host.name, "own": True,
                "off": not host.security_logs_enabled,
                "unhardened": not host.hardened,
            }
            for host in flags
        }
        for item in central.get("rows") or []:
            if not item.get("own") or not str(item.get("host_id")).isdigit():
                continue
            row = rows.get(int(item["host_id"]))
            if not row:
                continue
            for role in ("ssh_bruteforce", "weblogin_bruteforce"):
                if item.get(role):
                    row[role] = item[role]
            # A server whose switch the panel turned off stops sending:
            # its collector's last minutes are not a silence.
            if "silent" in item and not row["off"]:
                row["silent"] = True
        alerts = self.env["cloud.alert"].search([
            ("state", "=", "active"), ("code", "=like", "security\\_%"),
        ]).sudo()
        for alert in alerts:
            row = rows.get((alert.host_id or alert.instance_id.host_id).id)
            if not row:
                continue
            row["alerts"] = row.get("alerts", 0) + 1
            if row.get("level") != "critical":
                row["level"] = alert.level

        def weight(row):
            """Order the servers: critical alerts, then the most alerts,
            what is happening now, then what is set."""
            return (
                row.get("level") != "critical", -row.get("alerts", 0),
                not row.get("ssh_bruteforce"), not row.get("weblogin_bruteforce"),
                not row.get("silent"), not row["off"], not row["unhardened"],
                row["name"] or "",
            )

        listed = sorted(
            (r for r in rows.values() if r.get("alerts") or r.get("ssh_bruteforce")
             or r.get("weblogin_bruteforce") or r.get("silent") or r["off"]
             or r["unhardened"]),
            key=weight,
        )
        return {
            "state": "ready",
            "off": sum(1 for r in rows.values() if r["off"]),
            "unhardened": sum(1 for r in rows.values() if r["unhardened"]),
            "alerts_hosts": sum(1 for r in rows.values() if r.get("alerts")),
            "rows": listed[:_MAX_STATE_ROWS],
            "hidden": max(0, len(listed) - _MAX_STATE_ROWS),
        }

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
