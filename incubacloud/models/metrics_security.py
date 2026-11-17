"""A server's Security view: its counters, its defence and, for managers, its logs.

Plan #84, cut C6 (14-nov-2026), with decisions D2, D3 and D4 of the
owner. The server's Metrics tab has two views, Performance and
Security; this module answers the second (``/cloud/host/security_board``
and ``/cloud/host/security_lines``).

**Who sees what (D4), decided here and not on the screen.** Whoever may
read metrics (Developer and above) gets the figures: the charts of
``metrics_charts.PANELS["host_security"]``, which carry no address and
no user, the defence of the server, the HTTP of its instances and the
web logins' figures. The tables with addresses, users and commands, and
"see lines", go to whoever manages servers (``group_cloud_manager``)
alone: without that group the answer has no ``tables`` key at all, its
web-login figures name no address, and the lines are refused.

**How the tables are counted.** On the panel, over lines, never with an
aggregation by address in Loki: the last :data:`SAMPLE_PAGES` pages of a
window (:data:`SAMPLE_LINES` lines), counted by address (IPv6 by its
/64), as the web-login alert counts them (C2). So Loki's
``max_query_series`` never refuses a table during an attack from
thousands of addresses (review M4); a window that held more lines says
the counts are of its last ones. The aggregations reach 24 hours at most
whatever the board's range; the filtered reads (sign-ins, sudo, changes,
bans, one instance's web logins) reach 7 days.

**Countries** are looked up on the panel when the answer is built
(``geoip.py``, C3) and never kept; the answer carries the database's
attribution when it is DB-IP's.

**Never another account's.** Every query selects by the integer id of a
server the caller can read, through the account's credential, which the
central forces to the account; an instance named in a table is named
from the panel's database by its id, and an id that is not of an
instance of this server the caller can read is "unidentified".
"""
import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from odoo import models

from . import geoip, logs
from .cloud_metric_rule import central_busy
from .cloud_host_known_key import match_known_key
from .cloud_security_events import WATCHED_KEYS
from .metrics_charts import (
    CHART_RANGES,
    board_failure,
    chart_window,
    metrics_source,
    run_board,
)
from .metrics_diagnosis import diagnose_host_security

_logger = logging.getLogger(__name__)

#: Pages of 500 a table's sample reads at most, newest first: the last
#: 5 000 lines of its window (plan #84, C6).
SAMPLE_PAGES = 10
SAMPLE_LINES = SAMPLE_PAGES * logs.PAGE_LIMIT

#: Seconds an aggregation by address or user reaches back at most.
AGGREGATE_SECONDS = 24 * 3600

#: Seconds a filtered read (sign-ins, sudo, changes, bans, one
#: instance's web logins) reaches back.
DETAIL_SECONDS = 7 * 24 * 3600

#: Rows of an address table, and of a list of events, at most.
TOP_ROWS = 20
EVENT_ROWS = 50

#: Users tried a table names; the rest are counted.
USER_ROWS = 10

#: Lines "see lines" returns at most, newest first, and the characters
#: of each it keeps.
LINES_MAX = 200
LINE_CHARS = 2000

#: Seconds an answer is kept for the same person, server and window.
CACHE_SECONDS = 60
_CACHE_MAX = 256
_CACHE = {}
_CACHE_LOCK = threading.Lock()

#: Reads of one board run at once: the charts' own limit.
_READ_WORKERS = 4

#: The jails whose bans the tables join: SSH and the instances' web login.
SSH_JAIL = "sshd"
WEBLOGIN_JAIL = "incubacloud-weblogin"

#: What "see lines" may read, by name: nothing else reaches Loki.
LINE_SOURCES = {
    "sshd": "sshd",
    "sudo": "sudo",
    "fail2ban": "fail2ban",
    "auditd": "auditd",
    "weblogin": "weblogin",
    "all": "lines",
}

#: The code of the alert that says a server's hardening is behind.
_HARDENING_OUTDATED = "host_hardening_outdated"

_SECOND_NS = 1_000_000_000


def _iso(ns):
    """Return a Loki instant as ISO 8601 in UTC.

    :param int ns: nanoseconds since the epoch.
    :rtype: str
    """
    return datetime.fromtimestamp(ns / _SECOND_NS, tz=timezone.utc).isoformat()


def _naive(ns):
    """Return a Loki instant as the ORM's naive UTC datetime.

    :param int ns: nanoseconds since the epoch.
    :rtype: datetime
    """
    return datetime.fromtimestamp(ns / _SECOND_NS, tz=timezone.utc).replace(tzinfo=None)


def _positive_id(value):
    """Return *value* as a positive int id, or None.

    Only an int (never a bool, a string or a float): what the browser
    sends is an id or nothing.

    :rtype: int | None
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _cache_get(key):
    """Return a copy of the answer kept under *key*, or None.

    :param tuple key: who asks, about what and for which window.
    :rtype: dict | None
    """
    with _CACHE_LOCK:
        kept = _CACHE.get(key)
        if not kept:
            return None
        if kept[0] < time.monotonic():
            _CACHE.pop(key, None)
            return None
        return copy.deepcopy(kept[1])


def _cache_put(key, answer):
    """Keep *answer* under *key* for :data:`CACHE_SECONDS`.

    The oldest entries go first once :data:`_CACHE_MAX` are kept.

    :param tuple key: as :func:`_cache_get`.
    :param dict answer: the board.
    """
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            for old in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _CACHE_MAX // 4]:
                _CACHE.pop(old, None)
        _CACHE[key] = (time.monotonic() + CACHE_SECONDS, copy.deepcopy(answer))


def _sample(reader, expression, start_ns, end_ns, pages=SAMPLE_PAGES):
    """Read the last lines of a window: ``(lines, cut)``.

    :param logs.LogReader reader: the account's log route.
    :param str expression: LogQL of :data:`logs.LOGQL_LINES`.
    :param int start_ns: first instant, included.
    :param int end_ns: last instant, included.
    :param int pages: pages of 500 at most.
    :rtype: tuple[list[logs.LogLine], bool]
    :raises requests.RequestException: on transport or HTTP failure.
    """
    return logs.read_last_lines(
        reader.base_url, expression, start_ns, end_ns + 1, pages=pages,
        token=reader.token, user=reader.user, verify=reader.verify,
    )


def _read_error(exc):
    """Return the word for a read that failed: ``busy`` or ``unanswered``.

    :param Exception exc: what the read raised.
    :rtype: str
    """
    return "busy" if central_busy(exc) else "unanswered"


def ban_state(lines, jail):
    """Return the addresses a jail holds banned at the end of *lines*.

    An address whose last ``Ban`` (or ``Restore Ban``) has no ``Unban``
    after it, by source (IPv6 by its /64); the ``Ban`` lines with their
    instant, for the bans table.

    :param lines: :class:`logs.LogLine` of fail2ban, oldest first.
    :param str jail: the jail.
    :returns: ``(banned, bans)``: the set of sources banned now, and
        ``[(ns, address)]`` of every ban, oldest first.
    :rtype: tuple[set[str], list[tuple[int, str]]]
    """
    last = {}
    bans = []
    for line in lines:
        record = logs.parse_fail2ban(line.line)
        if not record or record["jail"] != jail:
            continue
        if record["action"] == "ban":
            last[record["ip"]] = "ban"
            bans.append((line.ns, record["ip"]))
        elif record["action"] == "unban":
            last[record["ip"]] = "unban"
    banned = {logs.source_key(ip) for ip, action in last.items() if action == "ban"}
    return banned, bans


def count_sources(lines, parse, instance_id=None):
    """Count the attempts of *lines* by source: what an address table shows.

    :param lines: :class:`logs.LogLine`, any order.
    :param parse: the parser of their source (``logs.parse_sshd`` or
        ``logs.parse_weblogin``); a line it does not read is left out.
    :param instance_id: with the web logins, the one instance kept.
    :returns: ``{source: {"attempts", "users": {user: n}, "instances":
        {id: n}, "last_ns"}}``.
    :rtype: dict
    """
    out = {}
    for line in lines:
        record = parse(line.line)
        if not record:
            continue
        if "kind" in record and record["kind"] != "failed":
            continue
        if instance_id is not None and record.get("instance_id") != instance_id:
            continue
        key = logs.source_key(record["ip"])
        entry = out.setdefault(key, {
            "attempts": 0, "users": {}, "instances": {}, "last_ns": 0,
        })
        entry["attempts"] += 1
        entry["last_ns"] = max(entry["last_ns"], line.ns)
        if record.get("user"):
            entry["users"][record["user"]] = entry["users"].get(record["user"], 0) + 1
        if "instance_id" in record:
            iid = record["instance_id"]
            entry["instances"][iid] = entry["instances"].get(iid, 0) + 1
    return out


def _ranked(counted, limit):
    """Return the sources of *counted*, most attempts first, at most *limit*.

    :rtype: list[tuple[str, dict]]
    """
    return sorted(counted.items(), key=lambda kv: (-kv[1]["attempts"], kv[0]))[:limit]


class CloudHost(models.Model):
    _inherit = "cloud.host"

    # ── The board ──────────────────────────────────────────────────────────

    def _security_board(self, range_key=None, start=None, end=None,
                        instance_id=None, detail=False):
        """Read this server's Security view: figures for all, tables for managers.

        States without reading the central: ``unavailable`` (this panel
        may not receive security logs, a managed panel until its
        manager's permission), observability off or without a central
        (``metrics_source``'s answer) and ``off`` (this server does not
        send them). Kept :data:`CACHE_SECONDS` per person, server,
        window, filter and *detail*.

        :param str range_key: one of ``CHART_RANGES``.
        :param start: zoom window start, unix seconds.
        :param end: zoom window end, unix seconds.
        :param instance_id: the web-login tables of one instance of this
            server, over 7 days; None for every instance, over 24 hours.
        :param bool detail: whether the caller manages servers: only
            then are the tables with addresses, users and commands read
            and returned.
        :returns: ``{"ok": True, "state": "ready", "start", "end",
            "step", "panels", "diagnosis", "defense", "weblogin",
            "behind_cdn", "detail", "tables"?, "attribution",
            "known_keys", "timing"}``,
            ``{"ok": True, "state": "unavailable" | "off", ...}``, or
            ``{"ok": False, "reason", "error"}``.
        :rtype: dict
        """
        self.ensure_one()
        if not self._security_logs_available():
            return {"ok": True, "state": "unavailable", "timing": {}}
        source = metrics_source(self.env)
        if not source["ok"]:
            return source
        if not self.security_logs_enabled:
            return {
                "ok": True, "state": "off", "defense": self._security_defense(),
                "timing": {},
            }
        # A key added or removed changes how the sign-ins are attributed:
        # its stamp is part of the cache key, and the card of known keys
        # is read on every call, never kept.
        key = (
            self.env.cr.dbname, self.env.uid, self.id, range_key, start, end,
            instance_id, bool(detail), self.env.lang,
            self._security_known_keys_stamp(),
        )
        answer = _cache_get(key)
        if not answer:
            answer = self._security_board_read(
                source, range_key, start, end, instance_id, bool(detail),
            )
            if answer.get("ok"):
                _cache_put(key, answer)
        if answer.get("ok"):
            answer = {**answer, "known_keys": self._security_known_keys_answer(bool(detail))}
        return answer

    def _security_board_read(self, source, range_key, start, end, instance_id, detail):
        """Read the counters and the logs of the board; never cached here.

        :param dict source: ``metrics_source``'s answer, ``ok``.
        :returns: as :meth:`_security_board`.
        :rtype: dict
        """
        win_start, win_end, _step = chart_window(range_key, start, end)
        board = run_board(
            "host_security",
            {"host_id": self.id, "range": max(60, win_end - win_start)},
            {"host"}, source["base"], source["user"], source["token"],
            range_key=range_key, start=start, end=end,
            verify=source.get("verify", True),
        )
        failure = board_failure(self.env, board, source["user"], source["token"])
        if failure:
            return failure
        names = self._security_instance_names(self._security_panel_ids(board))
        self._security_name_instances(board, names)
        reads = self._security_logs_read(instance_id, detail)
        lookup = geoip.resolver(self.env)
        countries = []

        def country(address):
            """The country of an address or /64, noted for the attribution."""
            code = lookup(address.split("/")[0])
            if code and code != geoip.LOCAL:
                countries.append(code)
            return code

        names.update(self._security_instance_names(self._security_read_ids(reads)))
        answer = {
            "ok": True,
            "state": "ready",
            **board,
            "defense": self._security_defense(),
            "behind_cdn": bool(self.behind_cdn),
            "detail": detail,
            "instance_filter": instance_id,
            "weblogin": self._security_weblogin_figures(board, reads, names, instance_id),
        }
        if detail:
            answer["tables"] = self._security_tables(reads, names, country, instance_id)
        answer["diagnosis"] = diagnose_host_security(answer, self, now=time.time())
        answer["attribution"] = lookup.attribution if countries else None
        return answer

    # ── Reading the logs ──────────────────────────────────────────────────

    def _security_logs_read(self, instance_id, detail):
        """Read the lines the board counts, in parallel, each on its own.

        Everyone's answer needs the web logins' sample (how many
        addresses, never which); a manager's also reads the failed SSH
        connections, the bans, the sign-ins, sudo and auditd. A read that
        fails leaves the others: its entry carries ``error``.

        :param instance_id: one instance's web logins over 7 days, or
            None for every instance's over 24 hours.
        :param bool detail: whether the manager's reads are made.
        :returns: ``{name: {"lines", "cut", "start_ns", "end_ns"} |
            {"error"}}``; ``{"_route": "missing"}`` without a log route.
        :rtype: dict
        """
        reader = logs.LogReader.from_env(self.env)
        if reader is None:
            return {"_route": "missing"}
        end_ns = time.time_ns()
        day = end_ns - AGGREGATE_SECONDS * _SECOND_NS
        week = end_ns - DETAIL_SECONDS * _SECOND_NS
        host = [self.id]
        if instance_id:
            web = (logs.logql_weblogin(self.id, instance_id), week, SAMPLE_PAGES)
        else:
            web = (logs.logql_lines("weblogin", host), day, SAMPLE_PAGES)
        plan = {"weblogin": web}
        if detail:
            plan.update({
                "failed": (logs.logql_lines("failed", host), day, SAMPLE_PAGES),
                "bans": (logs.logql_lines("f2b_bans", host), week, 4),
                "accepted": (logs.logql_lines("accepted", host), week, 1),
                "sudo": (logs.logql_lines("sudo", host), week, 1),
                "auditd": (logs.logql_lines("auditd", host), week, 2),
            })

        def run(item):
            """One read; never raises."""
            expression, since, pages = item
            try:
                lines, cut = _sample(reader, expression, since, end_ns, pages=pages)
            except Exception as exc:  # noqa: BLE001 — one table, reported
                _logger.warning(
                    "[security] a read of host %s failed: %s", self.id, exc,
                )
                return {"error": _read_error(exc)}
            return {"lines": lines, "cut": cut, "start_ns": since, "end_ns": end_ns}

        with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
            futures = {name: pool.submit(run, item) for name, item in plan.items()}
            return {name: future.result() for name, future in futures.items()}

    # ── Instances by id ───────────────────────────────────────────────────

    @staticmethod
    def _security_panel_ids(board):
        """Return the instance ids the counters named, as ints.

        :param dict board: ``run_board``'s answer.
        :rtype: set[int]
        """
        ids = set()
        for key in ("weblogin",):
            for series in board["panels"].get(key, {}).get("series") or []:
                if str(series.get("id") or "").isdigit():
                    ids.add(int(series["id"]))
        for row in board["panels"].get("http", {}).get("rows") or []:
            if str(row.get("id") or "").isdigit():
                ids.add(int(row["id"]))
        ids.discard(0)
        return ids

    @staticmethod
    def _security_read_ids(reads):
        """Return the instance ids the web logins' lines named.

        :param dict reads: :meth:`_security_logs_read`'s answer.
        :rtype: set[int]
        """
        ids = set()
        for line in (reads.get("weblogin") or {}).get("lines") or []:
            record = logs.parse_weblogin(line.line)
            if record and record["instance_id"]:
                ids.add(record["instance_id"])
        return ids

    def _security_instance_names(self, ids):
        """Return the names of the instances of this server the caller reads.

        An id that is not of an instance of this server, or of one the
        caller cannot read, is left out: it is shown "unidentified",
        never with the name of another instance.

        :param ids: instance ids.
        :returns: ``{id: name}``.
        :rtype: dict[int, str]
        """
        self.ensure_one()
        ids = [i for i in ids if _positive_id(i)]
        if not ids:
            return {}
        found = self.env["cloud.instance"].search([
            ("id", "in", ids), ("host_id", "=", self.id),
        ])
        return {inst.id: inst.name for inst in found}

    @staticmethod
    def _security_name_instances(board, names):
        """Name the counters' instances from the panel's database, in place.

        :param dict board: ``run_board``'s answer.
        :param dict names: :meth:`_security_instance_names`.
        """
        for series in board["panels"].get("weblogin", {}).get("series") or []:
            iid = int(series["id"]) if str(series.get("id") or "").isdigit() else 0
            series["name"] = names.get(iid) or ""
            series["unidentified"] = iid not in names
        for row in board["panels"].get("http", {}).get("rows") or []:
            iid = int(row["id"]) if str(row.get("id") or "").isdigit() else 0
            row["name"] = names.get(iid) or ""
            row["unidentified"] = iid not in names

    # ── Figures ───────────────────────────────────────────────────────────

    def _security_weblogin_figures(self, board, reads, names, instance_id):
        """Return the web logins' figures: what Developer and above see.

        Attempts per instance over the range (the counters), the jail's
        bans and lifts, attempts no instance was identified for, and how
        many addresses the last lines came from. No address.

        :rtype: dict
        """
        rows = board["panels"].get("http", {}).get("rows") or []
        instances = []
        unidentified = 0
        for row in rows:
            attempts = row.get("weblogin") or 0
            if not attempts:
                continue
            iid = int(row["id"]) if str(row.get("id") or "").isdigit() else 0
            if iid in names:
                instances.append({"id": iid, "name": names[iid], "attempts": attempts})
            else:
                unidentified += attempts
        instances.sort(key=lambda r: (-r["attempts"], r["name"]))
        now = board["panels"].get("now", {}).get("value") or {}
        figures = {
            "instances": instances,
            "unidentified": unidentified,
            "bans": now.get("weblogin_bans"),
            "unbans": now.get("weblogin_unbans"),
            "window_hours": (DETAIL_SECONDS if instance_id else AGGREGATE_SECONDS) // 3600,
        }
        if reads.get("_route"):
            figures["error"] = "no_route"
            return figures
        read = reads.get("weblogin") or {}
        if "error" in read:
            figures["error"] = read["error"]
            return figures
        counted = count_sources(read["lines"], logs.parse_weblogin, instance_id)
        figures.update({
            "distinct": len(counted),
            "sampled": sum(entry["attempts"] for entry in counted.values()),
            "cut": read["cut"],
            "sample": SAMPLE_LINES,
        })
        return figures

    def _security_defense(self):
        """Return whether the defence works, from what the panel knows.

        Addresses allowed to SSH (how many, never which), hardening and
        whether it is current, reached through a CDN with the origin
        closed or not, the switch, the last security line read, the
        credential the agents write with and whether the panel can tell
        its own key (a server it signs in to with a password, or a key
        with a passphrase, cannot: plan #84, A4).

        :rtype: dict
        """
        self.ensure_one()
        host = self.sudo()
        allowed = [p for p in (host.allowed_ssh_ips or "").replace("\n", ",").split(",") if p.strip()]
        outdated = self.env["cloud.alert"].sudo().search_count([
            ("code", "=", _HARDENING_OUTDATED), ("state", "=", "active"),
            ("host_id", "=", host.id),
        ])
        return {
            "ssh_allowlist": len(allowed),
            "hardened": bool(host.hardened),
            "hardening_current": bool(host.hardened) and not outdated,
            "behind_cdn": bool(host.behind_cdn),
            "origin_closed": bool(host.hardening_cdn_only),
            "enabled": bool(host.security_logs_enabled),
            "last_line_at": (
                host.security_last_line_at.replace(tzinfo=timezone.utc).isoformat()
                if host.security_last_line_at else None
            ),
            "write_user": host.metrics_write_user or None,
            "key_known": bool(host._panel_key_fingerprints()),
            "login_type": host.login_type,
            "weblogin_jail": not host.behind_cdn,
        }

    # ── Tables: managers only ─────────────────────────────────────────────

    def _security_tables(self, reads, names, country, instance_id):
        """Return the tables with addresses, users and commands.

        Called for a manager alone (D4). Each table carries ``error``
        when its read failed, ``cut`` and ``sample`` when its window
        held more lines than were read.

        :param dict reads: :meth:`_security_logs_read`'s answer.
        :param dict names: instance names by id.
        :param country: ``address -> ISO code | None``.
        :param instance_id: the web-login filter, or None.
        :rtype: dict
        """
        if reads.get("_route"):
            return {"error": "no_route"}
        bans_read = reads.get("bans") or {}
        bans_lines = bans_read.get("lines") or []
        ssh_banned, _ssh_bans = ban_state(bans_lines, SSH_JAIL)
        web_banned, web_bans = ban_state(bans_lines, WEBLOGIN_JAIL)
        banned_known = "error" not in bans_read
        tables = {
            "ssh_sources": self._security_ssh_sources(
                reads.get("failed") or {}, ssh_banned if banned_known else None, country,
            ),
            "accepted": self._security_accepted(reads.get("accepted") or {}, country),
            "sudo": self._security_sudo(reads.get("sudo") or {}),
            "changes": self._security_changes(reads.get("auditd") or {}),
        }
        web = reads.get("weblogin") or {}
        tables["weblogin_sources"] = self._security_weblogin_sources(
            web, web_banned if banned_known else None, names, country, instance_id,
        )
        tables["weblogin_bans"] = self._security_weblogin_bans(
            bans_read, web_bans, web, names, country, instance_id,
        )
        return tables

    @staticmethod
    def _instance_rows(instances, names):
        """Return ``[{"id", "name", "attempts"}]`` of an address's instances.

        :param dict instances: ``{id: attempts}``.
        :param dict names: instance names by id; one missing is None.
        :rtype: list[dict]
        """
        return [
            {"id": iid if iid in names else 0, "name": names.get(iid), "attempts": n}
            for iid, n in sorted(instances.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    def _security_ssh_sources(self, read, banned, country):
        """Return the SSH table: addresses with the most failures, and users tried.

        :param dict read: the ``failed`` read.
        :param banned: the sources the SSH jail holds now, or None
            when the bans could not be read.
        :rtype: dict
        """
        if "error" in read:
            return {"error": read["error"]}
        counted = count_sources(read.get("lines") or [], logs.parse_sshd)
        users = {}
        for entry in counted.values():
            for user, n in entry["users"].items():
                users[user] = users.get(user, 0) + n
        return {
            "rows": [
                {
                    "address": source, "country": country(source),
                    "attempts": entry["attempts"],
                    "blocked": None if banned is None else source in banned,
                    "last": _iso(entry["last_ns"]),
                }
                for source, entry in _ranked(counted, TOP_ROWS)
            ],
            "users": [
                {"user": user, "attempts": n}
                for user, n in sorted(users.items(), key=lambda kv: (-kv[1], kv[0]))[:USER_ROWS]
            ],
            "distinct_users": len(users),
            "distinct": len(counted),
            "cut": read.get("cut", False),
            "sample": SAMPLE_LINES,
        }

    def _security_weblogin_sources(self, read, banned, names, country, instance_id):
        """Return the web-login table: the addresses with the most attempts.

        :rtype: dict
        """
        if "error" in read:
            return {"error": read["error"]}
        counted = count_sources(read.get("lines") or [], logs.parse_weblogin, instance_id)
        return {
            "rows": [
                {
                    "address": source, "country": country(source),
                    "attempts": entry["attempts"],
                    "instances": self._instance_rows(entry["instances"], names),
                    "blocked": None if banned is None else source in banned,
                }
                for source, entry in _ranked(counted, TOP_ROWS)
            ],
            "cut": read.get("cut", False),
            "sample": SAMPLE_LINES,
        }

    def _security_weblogin_bans(self, bans_read, bans, web, names, country, instance_id):
        """Return the web-login jail's bans of 7 days, newest first.

        Each with the instances the address tried before it, from the web
        logins read (24 hours, or 7 days of one instance).

        :rtype: dict
        """
        if "error" in bans_read:
            return {"error": bans_read["error"]}
        tried = {}
        for line in web.get("lines") or []:
            record = logs.parse_weblogin(line.line)
            if record:
                tried.setdefault(logs.source_key(record["ip"]), []).append(
                    (line.ns, record["instance_id"]),
                )
        rows = []
        for ns, address in reversed(bans):
            source = logs.source_key(address)
            before = {}
            for at, iid in tried.get(source, ()):
                if at <= ns:
                    before[iid] = before.get(iid, 0) + 1
            if instance_id and instance_id not in before:
                continue
            rows.append({
                "at": _iso(ns), "address": address, "country": country(address),
                "instances": self._instance_rows(before, names),
            })
            if len(rows) >= EVENT_ROWS:
                break
        return {"rows": rows, "cut": bans_read.get("cut", False)}

    def _security_accepted(self, read, country):
        """Return the sign-ins of 7 days, newest first, each attributed.

        The attribution of the security events' cron (C4), without its
        confirming connection: the viewer only reads.

        - ``panel``: the panel's key (or, without one it can tell, its
          user) from an address it connects from;
        - ``panel_elsewhere``: the same from another address; the cron
          checks it with a connection of its own;
        - ``foreign_key``, ``password``, ``other_user``: someone else's;
        - ``known_key``: one of the server's known keys in its window,
          named by ``known_label``; ``known_key_elsewhere``: the same from
          an address it may not sign in from.

        :rtype: dict
        """
        if "error" in read:
            return {"error": read["error"]}
        events = self.env["cloud.security.events"]
        fingerprints = self.sudo()._panel_key_fingerprints()
        known = self.sudo()._security_known_keys()
        rows = []
        for line in reversed(read.get("lines") or []):
            record = logs.parse_sshd(line.line)
            if not record or record["kind"] != "accepted":
                continue
            attribution, label = self._security_attribute_login(
                events, record, fingerprints, line.ns, known,
            )
            rows.append({
                "at": _iso(line.ns), "user": record["user"],
                "address": record["ip"], "country": country(record["ip"]),
                "method": logs.sanitize(record["method"], 32),
                "key": "known" if label is not None else self._security_key_label(
                    record, fingerprints, line.ns,
                ),
                "attribution": attribution,
                "known_label": label,
            })
            if len(rows) >= EVENT_ROWS:
                break
        return {"rows": rows, "cut": read.get("cut", False)}

    def _security_key_label(self, record, fingerprints, ns):
        """Say which key signed in: the panel's, its previous one, another or none.

        :rtype: str
        """
        if not record["fingerprint"]:
            return "none"
        if record["fingerprint"] in fingerprints:
            return "panel"
        if self.env["cloud.security.events"]._security_previous_key(
            self, record["fingerprint"], ns,
        ):
            return "panel"
        return "other"

    def _security_attribute_login(self, events, record, fingerprints, ns, known=()):
        """Return whose a sign-in looks: the cron's table, without its connection.

        :param events: the ``cloud.security.events`` model.
        :param dict record: ``logs.parse_sshd``'s record.
        :param list fingerprints: the panel's key fingerprints here.
        :param int ns: the line's instant.
        :param known: the server's known keys, removed ones included: a
            removed key names the sign-ins of its window.
        :returns: ``(attribution, known key's name or None)``.
        :rtype: tuple
        """
        key = None
        if fingerprints:
            if not record["fingerprint"]:
                return "password", None
            if record["fingerprint"] not in fingerprints and not (
                events._security_previous_key(self, record["fingerprint"], ns)
            ):
                key = match_known_key(known, record["fingerprint"], _naive(ns))
                if key is None:
                    return "foreign_key", None
        else:
            key = match_known_key(known, record["fingerprint"], _naive(ns))
            if key is None and record["user"] != logs.sanitize(self.user, logs.USER_MAX):
                return "other_user", None
        if key is not None:
            return ("known_key" if key.allows(record["ip"]) else "known_key_elsewhere"), key.name
        if events._security_panel_address(self, record["ip"]):
            return "panel", None
        return "panel_elsewhere", None

    def _security_windows_of(self, read):
        """Return the panel's windows at work on this server over a read.

        :param dict read: a read with ``start_ns`` and ``end_ns``.
        :rtype: list[tuple[datetime, datetime]]
        """
        return self.env["cloud.security.events"]._security_windows(
            self, _naive(read["start_ns"]), _naive(read["end_ns"]),
        )

    @staticmethod
    def _in_windows(windows, ns):
        """Return whether *ns* falls in one of *windows*.

        :rtype: bool
        """
        at = _naive(ns)
        return any(begin <= at <= finish for begin, finish in windows)

    def _security_sudo(self, read):
        """Return the sudo commands of 7 days, newest first, without arguments.

        ``attribution``: ``panel_ansible`` (the panel's user with
        Ansible's mark, which only means something for that user),
        ``panel`` (the panel's user within a job, console or terminal of
        the panel), ``outside`` (the panel's user outside them) or
        ``other_user``.

        :rtype: dict
        """
        if "error" in read:
            return {"error": read["error"]}
        lines = read.get("lines") or []
        panel_user = logs.sanitize(self.user, logs.USER_MAX)
        windows = self._security_windows_of(read) if lines else []
        rows = []
        for line in reversed(lines):
            record = logs.parse_sudo(line.line)
            if not record:
                continue
            if record["user"] != panel_user:
                attribution = "other_user"
            elif record["become"]:
                attribution = "panel_ansible"
            elif self._in_windows(windows, line.ns):
                attribution = "panel"
            else:
                attribution = "outside"
            rows.append({
                "at": _iso(line.ns), "user": record["user"],
                "run_as": record["run_as"], "program": record["program"],
                "refused": record["kind"] == "refused",
                "attribution": attribution,
            })
            if len(rows) >= EVENT_ROWS:
                break
        return {"rows": rows, "cut": read.get("cut", False)}

    def _security_changes(self, read):
        """Return the changes to the watched files of 7 days, newest first.

        ``who`` as the cron reads auditd's login uid (system, panel, root
        or user); ``attribution``: ``system`` (no login: a package or a
        timer), ``panel`` (the panel's user or root within a job, console
        or terminal of the panel), ``outside`` (the same outside them) or
        ``other_user``.

        :rtype: dict
        """
        if "error" in read:
            return {"error": read["error"]}
        lines = read.get("lines") or []
        joined = logs.join_audit(lines, read["end_ns"], defer_seconds=0)
        events = self.env["cloud.security.events"]
        windows = self._security_windows_of(read) if joined.events else []
        rows = []
        for event in reversed(joined.events):
            if event.get("key") not in WATCHED_KEYS:
                continue
            who = events._security_who(self, event.get("auid"))
            if who == "system":
                attribution = "system"
            elif who == "user":
                attribution = "other_user"
            elif self._in_windows(windows, event["ns"]):
                attribution = "panel"
            else:
                attribution = "outside"
            rows.append({
                "at": _iso(event["ns"]), "file": event.get("file"),
                "who": who, "program": event.get("exe") or event.get("comm"),
                "attribution": attribution,
            })
            if len(rows) >= EVENT_ROWS:
                break
        return {"rows": rows, "cut": read.get("cut", False)}

    # ── See lines ─────────────────────────────────────────────────────────

    def _security_lines(self, source="all", range_key="24h", instance_id=None):
        """Return this server's last security lines of one source, raw.

        For whoever manages servers (the route checks it): the lines are
        what the attacker wrote and what sudo was given, secrets
        included, as the server sent them. At most :data:`LINES_MAX`,
        newest first, each cut to :data:`LINE_CHARS` characters.

        :param str source: a key of :data:`LINE_SOURCES`.
        :param str range_key: one of ``CHART_RANGES``: how far back.
        :param instance_id: with ``weblogin``, one instance of this
            server.
        :returns: ``{"ok": True, "lines": [{"at", "source", "line"}],
            "cut"}``, or ``{"ok": False, "error"}``.
        :rtype: dict
        """
        self.ensure_one()
        name = LINE_SOURCES.get(source) if isinstance(source, str) else None
        seconds = CHART_RANGES.get(range_key) if isinstance(range_key, str) else None
        if not name or not seconds:
            return {"ok": False, "error": self.env._("Unknown kind of security lines.")}
        if not self._security_logs_available() or not self.security_logs_enabled:
            return {"ok": False, "error": self.env._("This server does not send its security logs.")}
        reader = logs.LogReader.from_env(self.env)
        if reader is None:
            return {"ok": False, "error": self.env._("No metrics central is configured.")}
        if source == "weblogin" and instance_id:
            expression = logs.logql_weblogin(self.id, instance_id)
        else:
            expression = logs.logql_lines(name, [self.id])
        end_ns = time.time_ns()
        try:
            lines, cut = logs.read_last_lines(
                reader.base_url, expression,
                end_ns - min(seconds, DETAIL_SECONDS) * _SECOND_NS, end_ns + 1,
                pages=1, limit=LINES_MAX,
                token=reader.token, user=reader.user, verify=reader.verify,
            )
        except Exception as exc:  # noqa: BLE001 — said, not raised
            _logger.warning("[security] lines of host %s not read: %s", self.id, exc)
            return {"ok": False, "error": self.env._("The metrics central could not answer.")}
        return {
            "ok": True,
            "lines": [
                {"at": _iso(line.ns), "source": line.source, "line": line.line[:LINE_CHARS]}
                for line in reversed(lines)
            ],
            "cut": cut,
        }
