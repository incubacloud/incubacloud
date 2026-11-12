"""What a board says in plain words: which resource is short.

Fixed rules, evaluated on the panels the board has just read
(``metrics_charts.PANELS["instance"]`` for an instance,
``PANELS["host"]`` for a server) over the last 15 minutes of its
window: a condition and a sentence each, no model and no opaque
heuristic. Each threshold carries its reason and the date it was set;
they are tuned against real data in QA, so change them here and in
``docs/user/docs/reference/monitoring-panels.md`` together.

The database rules of the design (a lock wait held five minutes, a base
near its connection ceiling) need the per-instance PostgreSQL series of
a later collector; until those exist the board's "Queries now" reading
answers that question on demand.
"""
from operator import itemgetter

from odoo.tools.misc import formatLang


#: Seconds of the window the diagnosis reads: the last quarter of an hour.
DIAGNOSIS_SECONDS = 900

#: Share of scheduler periods in which odoo was refused CPU by its quota.
#: Above a quarter, requests queue behind the quota most of the time
#: (10-nov-2026, from the design of the instance board).
THROTTLED_PERCENT = 25

#: Load per core of the server above which it counts as saturated: two
#: runnable tasks per core is a queue on every core (10-nov-2026).
HOST_LOAD_PER_CORE = 2

#: Share of the whole server's CPU another instance must use to be named
#: as the cause (10-nov-2026).
NEIGHBOUR_PERCENT = 50

#: Time some task waited for memory (PSI "some"). Past a tenth, the
#: kernel is reclaiming pages in earnest (10-nov-2026).
MEMORY_PRESSURE_PERCENT = 10

#: Time some task waited for the disk (PSI "some"). Past a fifth, every
#: instance of the server feels it (10-nov-2026).
IO_PRESSURE_PERCENT = 20

#: p95 of request time, in seconds, above which requests count as slow.
#: Odoo itself paints a request's Python time red past 5 s; users notice
#: well before, and 2 s is where a page stops feeling immediate
#: (10-nov-2026).
SLOW_P95_SECONDS = 2.0

#: CPU of odoo and of the database, in percent of one core, below which
#: a slow request is not spending its time on them (10-nov-2026).
IDLE_CPU_PERCENT = 30

#: Memory in use on a server, in percent, above which its board names
#: the instance that uses most: the threshold of the
#: ``metrics_memory_high`` rule, so the board and the alert agree
#: (12-nov-2026).
HOST_MEMORY_PERCENT = 92

#: Root disk use of a server, in percent, above which its board names
#: the largest instance directory: the threshold of the
#: ``metrics_disk_critical`` rule (12-nov-2026).
HOST_ROOT_PERCENT = 90

#: Window end this close to now (seconds) counts as "now": a board zoomed
#: into the past describes the past and gets no diagnosis.
_NOW_SLACK_SECONDS = 300

_TONES = ("ok", "info", "warn", "bad")

_GIB = 1024 ** 3


def _recent(points, since):
    """Return the values of ``[[ts, value], ...]`` at or after *since*."""
    return [value for ts, value in points if ts >= since]


def _mean(values):
    """Return the mean of *values*, or None when there are none."""
    return sum(values) / len(values) if values else None


def _series(board, key, role=None, name=None):
    """Yield the series of a panel, by role and name when given."""
    for series in board["panels"].get(key, {}).get("series") or ():
        if role is not None and series.get("role") != role:
            continue
        if name is not None and series.get("name") != name:
            continue
        yield series


def _recent_mean(board, key, since, role=None, name=None):
    """Return the mean over the recent window of one panel's matching series.

    Several matching series (two roles share a name) are averaged
    together; None when nothing matched.
    """
    values = []
    for series in _series(board, key, role, name):
        values.extend(_recent(series["points"], since))
    return _mean(values)


def _last(board, key, role=None, name=None):
    """Return the last value of the first matching series, or None."""
    for series in _series(board, key, role, name):
        if series["points"]:
            return series["points"][-1][1]
    return None


def instance_facts(board, instance_id, workers):
    """Reduce a board to the numbers the rules read.

    :param dict board: ``run_board``'s answer for the instance scope.
    :param int instance_id: the instance the board is about.
    :param int workers: its configured HTTP workers.
    :rtype: dict
    """
    since = board["end"] - DIAGNOSIS_SECONDS
    host = board["panels"].get("host", {}).get("value") or {}
    cores = host.get("cores") or 0
    neighbour = None
    if cores:
        for series in _series(board, "neighbours"):
            if series.get("id") == str(instance_id):
                continue
            mean = _mean(_recent(series["points"], since))
            share = None if mean is None else mean / cores
            if share is not None and (neighbour is None or share > neighbour[1]):
                neighbour = (series["name"], share)
    odoo_cpu = _recent_mean(board, "cpu", since, "use", "odoo")
    # The moments of the last 15 minutes only, as the rest: an
    # out-of-memory kill of yesterday on the 7-day range is history.
    events = [m for m in board["panels"].get("events", {}).get("markers") or ()
              if m["ts"] >= since]
    restarts = [m["ts"] for m in events
                if m["role"] == "restart" and m["name"] == "odoo"]
    quota = _last(board, "cpu", "limit", "odoo")
    return {
        "since": since,
        "throttled": _recent_mean(board, "throttled", since, "use", "odoo"),
        "odoo_cpu": odoo_cpu,
        "db_cpu": _recent_mean(board, "cpu", since, "use", "db"),
        "odoo_cpus": round(quota / 100, 2) if quota else None,
        "cores": cores,
        "load_per_core": (host.get("load15") or 0) / cores if cores else None,
        "neighbour": neighbour,
        "memory_pressure": _recent_mean(board, "pressure", since, "memory"),
        "io_pressure": _recent_mean(board, "pressure", since, "io"),
        "oom": any(m["role"] == "oom" for m in events),
        "last_restart": max(restarts) if restarts else None,
        "p95": _recent_mean(board, "latency", since, "p95"),
        "occupancy": (
            round(odoo_cpu / workers, 1) if odoo_cpu is not None and workers
            else None
        ),
    }


def _findings(env, facts, now):
    """Return ``[(code, tone, text)]`` for every rule that holds."""
    found = []
    throttled = facts["throttled"]
    if throttled is not None and throttled > THROTTLED_PERCENT:
        cpus = facts["odoo_cpus"]
        found.append(("cpu_throttled", "warn", env._(
            "Odoo is held back by its CPU quota (%(cpus)s CPUs) %(pct)s%% of "
            "the time. Raise the quota in Resources or spread the "
            "instances over more servers.",
            cpus=f"{cpus:g}" if cpus is not None else "?", pct=round(throttled),
        )))
    neighbour = facts["neighbour"]
    load = facts["load_per_core"]
    if (load is not None and load > HOST_LOAD_PER_CORE and neighbour
            and neighbour[1] > NEIGHBOUR_PERCENT):
        found.append(("host_neighbour", "warn", env._(
            "The server is saturated and most of it goes to another "
            "instance (%(name)s, %(pct)s%%). This instance is not the cause.",
            name=neighbour[0], pct=round(neighbour[1]),
        )))
    memory = facts["memory_pressure"]
    if facts["oom"] or (memory is not None and memory > MEMORY_PRESSURE_PERCENT):
        found.append(("memory_short", "bad", env._(
            "Memory is short: the server is evicting pages, or the kernel "
            "killed processes of this instance. Raise the instance's memory "
            "or lower its workers.",
        )))
    io = facts["io_pressure"]
    if io is not None and io > IO_PRESSURE_PERCENT:
        found.append(("io_pressure", "warn", env._(
            "The server's disk is the bottleneck. If a backup or a migration "
            "is running, wait for it to end; otherwise the server needs a "
            "faster disk.",
        )))
    p95 = facts["p95"]
    if (p95 is not None and p95 > SLOW_P95_SECONDS
            and (facts["odoo_cpu"] or 0) < IDLE_CPU_PERCENT
            and (facts["db_cpu"] or 0) < IDLE_CPU_PERCENT):
        found.append(("slow_idle", "warn", env._(
            "Requests take %(p95)s s (p95) without using CPU or the "
            "database: usually an outside call (mail, an API, a payment "
            "provider). See the slow routes.",
            p95=round(p95, 1),
        )))
    restart = facts["last_restart"]
    if restart is not None:
        found.append(("restarted", "info", env._(
            "Odoo restarted %(minutes)s minutes ago; the first minutes after "
            "a start are slow.",
            minutes=max(0, round((now - restart) / 60)),
        )))
    return found


def diagnose_instance(board, instance, workers, now):
    """Return the board's diagnosis: an overall tone and its sentences.

    :param dict board: ``run_board``'s answer for the instance scope.
    :param instance: the ``cloud.instance`` record (its env translates).
    :param int workers: its configured HTTP workers.
    :param float now: current time, unix seconds.
    :returns: ``{"current": bool, "tone", "findings": [{code, tone,
        text}], "facts": {...}}``; ``current`` is False, with no
        findings, when the window ends in the past.
    :rtype: dict
    """
    facts = instance_facts(board, instance.id, workers)
    window = max(_NOW_SLACK_SECONDS, 2 * board["step"])
    if now - board["end"] > window:
        return {"current": False, "tone": "ok", "findings": [], "facts": facts}
    found = _findings(instance.env, facts, now)
    if not [f for f in found if f[1] != "info"]:
        found.append(("nothing_saturated", "ok", instance.env._(
            "No resource looks saturated. If one screen is slow, the slow "
            "routes point at it.",
        )))
    tone = max((f[1] for f in found), key=_TONES.index)
    return {
        "current": True,
        "tone": tone,
        "findings": [{"code": c, "tone": t, "text": x} for c, t, x in found],
        "facts": {k: v for k, v in facts.items() if k != "since"},
    }


def _top(rows, key):
    """Return the row with the largest *key*, or None when none has it."""
    rows = [row for row in rows if row.get(key) is not None]
    return max(rows, key=itemgetter(key)) if rows else None


def host_facts(board):
    """Reduce a server's board to the numbers its rules read.

    :param dict board: ``run_board``'s answer for the host scope, its
        ``consumers`` rows already matched to instances.
    :rtype: dict
    """
    since = board["end"] - DIAGNOSIS_SECONDS
    host = board["panels"].get("host", {}).get("value") or {}
    rows = board["panels"].get("consumers", {}).get("rows") or []
    return {
        "memory": _recent_mean(board, "memory", since, "used"),
        "memory_total": host.get("memory_total"),
        "root": _recent_mean(board, "root", since, "used"),
        "load_per_core": _recent_mean(board, "load", since, "load15"),
        "cores": host.get("cores") or 0,
        "memory_pressure": _recent_mean(board, "pressure", since, "memory"),
        "io_pressure": _recent_mean(board, "pressure", since, "io"),
        "top_memory": _top(rows, "memory"),
        "top_cpu": _top(rows, "cpu"),
        "top_disk": _top(rows, "disk"),
    }


def _gib(env, value):
    """Return bytes as GB with one decimal, in the reader's language."""
    return formatLang(env, (value or 0) / _GIB, digits=1)


def _host_findings(env, facts):
    """Return ``[(code, tone, text)]`` for every server rule that holds."""
    found = []
    memory = facts["memory"]
    if memory is not None and memory > HOST_MEMORY_PERCENT:
        top = facts["top_memory"]
        if top and facts["memory_total"]:
            text = env._(
                "Memory is at %(pct)s%%: %(name)s uses the most, %(used)s GB "
                "of %(total)s GB. Lower its workers, give it less memory or "
                "move an instance to another server.",
                pct=round(memory), name=top["name"],
                used=_gib(env, top["memory"]),
                total=_gib(env, facts["memory_total"]),
            )
        else:
            text = env._(
                "Memory is at %(pct)s%%. Lower the workers of its instances "
                "or move one to another server.",
                pct=round(memory),
            )
        found.append(("memory_high", "warn", text))
    pressure = facts["memory_pressure"]
    if pressure is not None and pressure > MEMORY_PRESSURE_PERCENT:
        found.append(("memory_short", "bad", env._(
            "Tasks wait for memory %(pct)s%% of the time: the server is "
            "evicting pages from every instance. Move an instance to another "
            "server or give this one more memory.",
            pct=round(pressure),
        )))
    root = facts["root"]
    if root is not None and root > HOST_ROOT_PERCENT:
        top = facts["top_disk"]
        if top:
            text = env._(
                "The root disk is %(pct)s%% full; the largest instance "
                "directory is %(name)s (%(size)s GB). Free space or grow the "
                "disk before it fills up: a full disk stops every instance.",
                pct=round(root), name=top["name"], size=_gib(env, top["disk"]),
            )
        else:
            text = env._(
                "The root disk is %(pct)s%% full. Free space or grow the disk "
                "before it fills up: a full disk stops every instance.",
                pct=round(root),
            )
        found.append(("root_full", "bad", text))
    load = facts["load_per_core"]
    if load is not None and load > HOST_LOAD_PER_CORE:
        top = facts["top_cpu"]
        if top and facts["cores"]:
            text = env._(
                "The server is saturated (load %(load)s per core); %(name)s "
                "uses the most CPU, %(pct)s%% of the server.",
                load=formatLang(env, load, digits=1), name=top["name"],
                pct=round(top["cpu"] / facts["cores"]),
            )
        else:
            text = env._(
                "The server is saturated (load %(load)s per core).",
                load=formatLang(env, load, digits=1),
            )
        found.append(("cpu_saturated", "warn", text))
    io = facts["io_pressure"]
    if io is not None and io > IO_PRESSURE_PERCENT:
        found.append(("io_pressure", "warn", env._(
            "The disk is the bottleneck: tasks wait for it %(pct)s%% of the "
            "time. A backup or a migration running explains it; otherwise "
            "the server needs a faster disk.",
            pct=round(io),
        )))
    return found


def diagnose_host(board, host, now):
    """Return a server board's diagnosis: an overall tone and its sentences.

    :param dict board: ``run_board``'s answer for the host scope.
    :param host: the ``cloud.host`` record (its env translates).
    :param float now: current time, unix seconds.
    :returns: ``{"current": bool, "tone", "findings": [{code, tone,
        text}]}``; ``current`` is False, with no findings, when the
        window ends in the past.
    :rtype: dict
    """
    window = max(_NOW_SLACK_SECONDS, 2 * board["step"])
    if now - board["end"] > window:
        return {"current": False, "tone": "ok", "findings": []}
    found = _host_findings(host.env, host_facts(board))
    if not found:
        found.append(("normal", "ok", host.env._(
            "Normal: no resource of this server looks saturated.",
        )))
    tone = max((f[1] for f in found), key=_TONES.index)
    return {
        "current": True,
        "tone": tone,
        "findings": [{"code": c, "tone": t, "text": x} for c, t, x in found],
    }
