"""Where an instance's time went: its slowest routes, crons and kills.

Read on demand from the instance's own Odoo log (``scripts/
read_odoo_log.sh`` projects it on the host) and added up here, so what
reaches the browser is a table and not the log. Odoo appends to every
request it logs the number of queries, the seconds spent in SQL and the
seconds spent in Python (``PerfFilter`` in ``odoo/netsvc.py``, 11.0 on);
an older series logs no timing and its requests are only counted.

Nothing is stored: the log names the instance's users' paths.
"""
import re
from operator import itemgetter
from datetime import datetime, timedelta, timezone

from odoo import _, fields, models
from odoo.exceptions import UserError

#: The windows the panel offers, in minutes.
SLOW_ROUTE_WINDOWS = (15, 60)

#: Rows of each table the answer carries.
_TOP = 10

#: A path segment that is a record id, a hash or a token: it would make
#: every record its own route.
_VOLATILE_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{16,}|[0-9a-f-]{32,36})$")

#: How a scheduled action's lines name it, across series. 17.0 on:
#: ``Job 'Name' (12) starting`` and ``Job 'Name' (12) fully done (…;
#: duration 1.23s)``; before: ``Starting job `Name`.`` and
#: ``Job `Name` done.`` (or ``Job done: `Name` (1.234s).``).
_CRON_START = re.compile(r"^(?:Job '(?P<a>.+)' \(\d+\) starting|Starting job `(?P<b>.+)`\.)")
_CRON_DONE = re.compile(
    r"^(?:Job '(?P<a>.+)' \(\d+\) (?:fully done|partially done|failed|done)\b.*"
    r"|Job `(?P<b>.+)` done\.?.*"
    r"|Job done: `(?P<c>.+)`.*)$"
)
_CRON_DURATION = re.compile(r"(?:duration |\()(?P<s>\d+(?:\.\d+)?)s\)")

#: Odoo's log stamp, as the script passes it.
_STAMP = "%Y-%m-%d %H:%M:%S"


def _float(value):
    """Return *value* as a float, or None when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(stamp):
    """Return a log stamp (UTC) as ISO 8601 with its zone."""
    return stamp.replace(" ", "T") + "Z"


def route_key(request_line):
    """Return the route a logged request counts under.

    ``POST /web/dataset/call_kw/sale.order/read#sale.order.read HTTP/1.1``
    counts as ``POST /web/dataset/call_kw/sale.order/read#sale.order.read``;
    the query string goes, and an id, a hash or a token in the path
    becomes ``:id``, so ``/web/image/res.partner/5/avatar_128`` and its
    neighbours are one route.

    :param str request_line: ``METHOD PATH PROTO`` as Odoo logs it.
    :rtype: str
    """
    parts = (request_line or "").split()
    if len(parts) < 2:
        return request_line or "?"
    method, path = parts[0], parts[1]
    fragment = ""
    if "#" in path:
        path, fragment = path.split("#", 1)
        fragment = "#" + fragment
    path = path.split("?", 1)[0]
    segments = [
        ":id" if _VOLATILE_SEGMENT.match(seg) else seg
        for seg in path.split("/")
    ]
    return f"{method} {'/'.join(segments)}{fragment}"


def _percentile(values, share):
    """Return the nearest-rank percentile of non-empty *values*."""
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(share * len(ordered) + 0.5) - 1))
    return ordered[rank]


def parse_slow_routes(raw, minutes):
    """Add up the projected log lines into the tables the panel shows.

    :param str raw: ``read_odoo_log.sh``'s output.
    :param int minutes: the window read, for the recycle rate.
    :returns: ``{"routes": [...], "crons": [...], "kills": [...],
        "recycles": {"count", "per_hour"}, "requests", "untimed"}``.
    :rtype: dict
    """
    routes, crons, running, kills = {}, {}, {}, []
    recycles = requests = untimed = 0
    for line in (raw or "").splitlines():
        kind, _sep, rest = line.partition("\t")
        cols = rest.split("\t")
        if kind == "R" and len(cols) >= 6:
            requests += 1
            sql, python = _float(cols[4]), _float(cols[5])
            queries = _float(cols[3])
            row = routes.setdefault(route_key(cols[1]), {
                "count": 0, "times": [], "sql": 0.0, "python": 0.0,
                "queries": 0.0,
            })
            row["count"] += 1
            if sql is None or python is None:
                untimed += 1
                continue
            row["times"].append(sql + python)
            row["sql"] += sql
            row["python"] += python
            row["queries"] += queries or 0
        elif kind == "C" and len(cols) >= 3:
            stamp, pid, message = cols[0], cols[1], cols[2]
            started = _CRON_START.match(message)
            if started:
                name = started.group("a") or started.group("b")
                running[pid] = (name, stamp)
                continue
            done = _CRON_DONE.match(message)
            if not done:
                continue
            name = done.group("a") or done.group("b") or done.group("c")
            duration = _CRON_DURATION.search(message)
            seconds = _float(duration.group("s")) if duration else None
            begun = running.pop(pid, None)
            if seconds is None and begun and begun[0] == name:
                seconds = (
                    datetime.strptime(stamp, _STAMP)
                    - datetime.strptime(begun[1], _STAMP)
                ).total_seconds()
            row = crons.setdefault(name, {"runs": 0, "total": 0.0, "max": 0.0})
            row["runs"] += 1
            if seconds is not None:
                row["total"] += seconds
                row["max"] = max(row["max"], seconds)
        elif kind == "M":
            recycles += 1
        elif kind == "K" and len(cols) >= 2:
            kills.append({"at": _iso(cols[0]), "message": cols[1], "trace": []})
        elif kind == "+" and kills and len(kills[-1]["trace"]) < 10:
            kills[-1]["trace"].append(rest)

    table = []
    for key, row in routes.items():
        times = row["times"]
        total = sum(times)
        table.append({
            "route": key,
            "count": row["count"],
            "total": round(total, 3),
            "mean": round(total / len(times), 3) if times else None,
            "p95": round(_percentile(times, 0.95), 3) if times else None,
            "sql_share": round(100 * row["sql"] / total) if total else None,
            "python_share": (
                round(100 * row["python"] / total) if total else None
            ),
            "queries": round(row["queries"] / len(times)) if times else None,
        })
    table.sort(key=itemgetter("total", "count"), reverse=True)
    cron_table = [
        {"name": name, "runs": row["runs"], "total": round(row["total"], 1),
         "max": round(row["max"], 1), "running_since": None}
        for name, row in crons.items()
    ]
    cron_table += [
        {"name": name, "runs": 0, "total": None, "max": None,
         "running_since": _iso(stamp)}
        for name, stamp in running.values()
    ]
    cron_table.sort(
        key=lambda r: (r["running_since"] is None, -(r["max"] or 0)),
    )
    return {
        "requests": requests,
        "untimed": untimed,
        "routes": table[:_TOP],
        "crons": cron_table[:_TOP],
        "kills": kills[-_TOP:],
        "recycles": {
            "count": recycles,
            "per_hour": round(recycles * 60 / minutes, 1) if minutes else 0,
        },
    }


class CloudInstance(models.Model):
    _inherit = "cloud.instance"

    def _slow_routes_args(self, minutes):
        """Return ``(cutoff, minutes)`` for reading the last *minutes*.

        :param minutes: one of ``SLOW_ROUTE_WINDOWS``.
        :raises UserError: for any other window.
        """
        self.ensure_one()
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 0
        if minutes not in SLOW_ROUTE_WINDOWS:
            raise UserError(_("Unknown window for the slow routes."))
        cutoff = (
            fields.Datetime.now() - timedelta(minutes=minutes)
        ).strftime(_STAMP)
        return cutoff, minutes

    def _require_deployed_host(self):
        """Return the host to read this instance from, or say why not.

        :raises UserError: when the instance is on no host or not
            deployed, so it has no log nor database to read.
        """
        self.ensure_one()
        host = self._require_access_log_host()
        if not self.deployed:
            raise UserError(_(
                "This instance is not deployed, so there is nothing to read "
                "on its host yet."
            ))
        return host

    @staticmethod
    def _now_iso():
        """Return the current time as ISO 8601 UTC, for "read at"."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
