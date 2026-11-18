"""The servers' security logs read back from the central's Loki.

F3 of the observability plan sends each server's sshd, sudo, fail2ban
and auditd lines to the central's Loki, labelled ``log_type="security"``,
``host_id``, ``host`` and ``source``; the line is the message alone (the
journal's ``MESSAGE``, or the file's line). This module is the panel's
half: it reads them over LogQL and turns each line into a record.

**Reading.** Through the account's log read route, ``/lr``, beside the
``/r`` that ``metrics_central_url`` names, with the account's credential
and the transport of the metrics reads (``_promql_get``: one retry after
a transport failure or a 5xx, one after a pause on a 429). Pages are
taken by time: each starts at the last instant of the one before,
included, and a line read twice counts once. The last lines of a long
window are read in tranches of :data:`TRANCHE_SECONDS`, newest first, so
no request makes Loki queue more subqueries than it holds.

**Parsing.** Every parser is anchored at the start of the line and
returns ``None`` for a shape it does not know. Users tried and commands
are text the attacker writes: an attempted user can read ``Accepted
publickey for root from …``, and any local user can write a ``sudo``
line with ``logger -t sudo``. What a record carries is cut to a length
and to a closed set of characters (:func:`sanitize`) before it can reach
a message, and a record never carries the line itself: the line stays
on its :class:`LogLine`, for whoever is allowed to see lines.

**The instances' failed web logins.** A server's Fluent Bit sends one
line of its own per failed login, ``weblogin failed ip=… instance=…
t=…`` (C1.9), with ``source="weblogin"``. :func:`count_weblogin_sources`
reads the last of them for one instance and counts them by address on
the panel: lines, never an aggregation by address, so it needs no
series per address anywhere.

**auditd.** A change is a ``SYSCALL`` record and the ``PATH`` records
with the same ``audit(seconds:serial)``. They are joined once every page
is read (:func:`join_audit`), by ``host_id`` and that stamp, never by
the line. A ``SYSCALL`` whose ``PATH`` has not arrived within the last
minute of the window is left for the next pass, whose overlap brings it
whole.

Plan #84, cut C2 (13-nov-2026), with Fable's review: M5 and point 6;
the web logins of 14-nov-2026 (D3, D5).
"""
import ipaddress
import logging
import re
import time
from typing import NamedTuple

from .cloud_metric_rule import _promql_get

_logger = logging.getLogger(__name__)

#: Seconds a log read may take: the charts' own limit.
QUERY_TIMEOUT = 8

#: Lines one page asks for.
PAGE_LIMIT = 500

#: Seconds of a window one request of :func:`read_last_lines` reaches at
#: most. Loki splits a request into subqueries and answers 429 to an
#: account whose subqueries fill its queue: as it comes (2.9), every 30
#: minutes and 100 queued, so seven days in one request (336) were
#: refused, even alone (QA of 15-nov-2026). Twelve hours are 24 there,
#: and a board's four reads at once (``metrics_security._READ_WORKERS``)
#: stay under 100; on a central this version deploys, which splits by
#: day, one or two. Measured on Loki 2.9.8 with a week of lines.
TRANCHE_SECONDS = 12 * 3600

#: Pages one read takes at most before it stops and says so.
MAX_PAGES = 10

#: A ``SYSCALL`` this close to the end of the window, in seconds, with no
#: ``PATH`` yet, waits for the next pass.
AUDIT_DEFER_SECONDS = 60

#: The suffix of ``metrics_central_url`` on a central this panel deploys,
#: and the log read route beside it.
_ACCOUNT_READ_SUFFIX = "/r"
_LOG_READ_ROOT = "/lr/loki"

# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class LogLine(NamedTuple):
    """One line Loki returned: whose, when and what.

    :ivar host_id: the ``cloud.host`` id of the ``host_id`` label, or
        None when the line has none (lines sent before it existed).
    :ivar ns: Loki's timestamp, nanoseconds since the epoch.
    :ivar line: the line as the server sent it. Raw: never into a
        message or an alert's payload.
    :ivar source: the ``source`` label (sshd, sudo, fail2ban, auditd).
    """

    host_id: int | None
    ns: int
    line: str
    source: str


class LogRead(NamedTuple):
    """What :func:`read_lines` read.

    :ivar lines: every distinct line, oldest first.
    :ivar last_ns: the instant of the last line read, or None when there
        was none.
    :ivar complete: True when the window was read to its end; False when
        the reading stopped at its page limit, so the lines after
        ``last_ns`` are still to read.
    """

    lines: list
    last_ns: int | None
    complete: bool


def loki_base(base_url):
    """Return the log read root that goes with *base_url*, or None.

    ``metrics_central_url`` of a central this panel deploys ends in the
    account read prefix ``/r``; the log read route is ``/lr`` beside it.
    Any other URL is a backend that is not ours and has no log store we
    know of.

    :param str base_url: ``metrics_central_url``.
    :returns: the URL ``_promql_get`` appends ``/api/v1/<endpoint>`` to.
    :rtype: str | None
    """
    root = (base_url or "").strip().rstrip("/")
    if not root.endswith(_ACCOUNT_READ_SUFFIX):
        return None
    return root[: -len(_ACCOUNT_READ_SUFFIX)] + _LOG_READ_ROOT


def _host_id(labels):
    """Return the ``host_id`` label of a stream as an int, or None.

    :param dict labels: the stream's labels.
    :rtype: int | None
    """
    raw = str((labels or {}).get("host_id") or "")
    return int(raw) if raw.isdigit() else None


def logql_query_range(
    base_url, expression, start_ns, end_ns, limit=PAGE_LIMIT,
    timeout=QUERY_TIMEOUT, token=None, user=None, retries=1, verify=True,
    direction="forward",
):
    """Run one LogQL log query and return its lines, oldest first.

    One page: Loki gives the first *limit* lines of the window across
    every stream, or the last with ``direction="backward"``; the window
    holds *start_ns* and not *end_ns*. :func:`read_lines` takes the
    pages that follow.

    :param str base_url: the log read root (:func:`loki_base`).
    :param str expression: LogQL selecting lines.
    :param int start_ns: first instant, nanoseconds, included.
    :param int end_ns: last instant, nanoseconds.
    :param int limit: lines at most.
    :param token: password half of the account's credential.
    :param user: its user, whose account the central filters by.
    :param int retries: extra attempts after a transport failure, a 5xx
        or a 429.
    :param verify: the central's certificate check
        (``cloud.settings._central_tls_verify``).
    :param str direction: ``forward`` (the first lines of the window) or
        ``backward`` (its last).
    :returns: the lines, oldest first.
    :rtype: list[LogLine]
    :raises requests.RequestException: on transport or HTTP failure.
    :raises ValueError: when Loki answers with an error.
    """
    payload = _promql_get(
        base_url, "query_range",
        {"query": expression, "start": int(start_ns), "end": int(end_ns),
         "limit": int(limit),
         "direction": "backward" if direction == "backward" else "forward"},
        timeout=timeout, token=token, user=user, retries=retries,
        verify=verify,
    )
    lines = []
    for stream in payload.get("data", {}).get("result", []):
        labels = stream.get("stream") or {}
        host_id = _host_id(labels)
        source = str(labels.get("source") or "")
        for raw_ns, line in stream.get("values") or []:
            try:
                ns = int(raw_ns)
            except (TypeError, ValueError):
                continue
            lines.append(LogLine(host_id, ns, str(line), source))
    lines.sort(key=lambda item: (item.ns, item.host_id or 0, item.line))
    return lines


def logql_query(
    base_url, expression, at_ns=None, timeout=QUERY_TIMEOUT, token=None,
    user=None, retries=1, verify=True,
):
    """Run a LogQL metric query at one instant: ``[(labels, value), ...]``.

    For aggregations (``sum by (…) (count_over_time(…))``); Loki answers
    a vector, as PromQL does. Same transport and credential as
    :func:`logql_query_range`.

    :param str base_url: the log read root (:func:`loki_base`).
    :param str expression: a LogQL metric expression.
    :param at_ns: the instant, nanoseconds; now when None.
    :rtype: list[tuple[dict, float]]
    """
    params = {"query": expression}
    if at_ns is not None:
        params["time"] = int(at_ns)
    payload = _promql_get(
        base_url, "query", params,
        timeout=timeout, token=token, user=user, retries=retries,
        verify=verify,
    )
    samples = []
    for item in payload.get("data", {}).get("result", []):
        value = item.get("value") or [None, None]
        try:
            samples.append((item.get("metric", {}), float(value[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return samples


def read_lines(
    base_url, expression, start_ns, end_ns, max_pages=MAX_PAGES,
    limit=PAGE_LIMIT, timeout=QUERY_TIMEOUT, token=None, user=None,
    verify=True,
):
    """Read a window page after page, by time, and return what it holds.

    Each page starts at the last instant of the one before, included:
    Loki may have cut a page in the middle of an instant shared by
    several lines. A line read twice, the same server, instant and text,
    counts once. A full page that adds nothing (more than *limit* lines
    at one instant) moves on one nanosecond, and says so in the log.

    :param str base_url: the log read root (:func:`loki_base`).
    :param str expression: LogQL selecting lines.
    :param int start_ns: first instant, nanoseconds, included.
    :param int end_ns: last instant, nanoseconds.
    :param int max_pages: pages at most.
    :param int limit: lines a page asks for.
    :rtype: LogRead
    :raises requests.RequestException: as :func:`logql_query_range`; what
        earlier pages read is not returned, so a caller never takes a
        window read in part for one read whole.
    :raises ValueError: as :func:`logql_query_range`.
    """
    seen = set()
    lines = []
    cursor = int(start_ns)
    for _page in range(max_pages):
        page = logql_query_range(
            base_url, expression, cursor, end_ns, limit=limit,
            timeout=timeout, token=token, user=user, verify=verify,
        )
        fresh = [item for item in page if item[:3] not in seen]
        for item in fresh:
            seen.add(item[:3])
        lines.extend(fresh)
        if len(page) < limit:
            return _read(lines, complete=True)
        last = page[-1].ns
        if not fresh:
            _logger.warning(
                "[logs] more than %d lines at one instant (%d): the rest "
                "of that instant is not read", limit, last,
            )
            last += 1
        cursor = last
        if cursor >= end_ns:
            return _read(lines, complete=True)
    return _read(lines, complete=False)


def _read(lines, complete):
    """Return a :class:`LogRead` of *lines*, put in time order.

    :param list lines: distinct lines, in the order they were read.
    :param bool complete: whether the window was read to its end.
    :rtype: LogRead
    """
    lines.sort(key=lambda item: (item.ns, item.host_id or 0, item.line))
    return LogRead(lines, lines[-1].ns if lines else None, complete)


# ---------------------------------------------------------------------------
# The closed LogQL: data, as the charts' PANELS are
# ---------------------------------------------------------------------------

#: The line queries the panel runs, by name. Nothing else reaches Loki:
#: a browser sends a name and host ids, never LogQL. ``%(hosts)s`` is the
#: ``host_id`` matcher :func:`logql_lines` writes from integers.
LOGQL_LINES = {
    # Correct logins, the panel's own included.
    "accepted":
        '{log_type="security", source="sshd", %(hosts)s} |~ "^Accepted "',
    # The one line sshd ends each failed connection with (classify.lua).
    "failed":
        '{log_type="security", source="sshd", %(hosts)s}'
        ' |~ "^(Connection closed by|Connection reset by|Disconnected from'
        '|Disconnecting) (invalid|authenticating) user "',
    # Commands run with sudo, and the refused ones (they carry COMMAND=).
    "sudo":
        '{log_type="security", source="sudo", %(hosts)s} |~ "COMMAND="',
    # The changes to the watched files: SYSCALL and PATH records.
    "auditd":
        '{log_type="security", source="auditd", %(hosts)s}'
        ' |~ "^type=(SYSCALL|PATH) "',
    # fail2ban's findings, bans and lifts.
    "fail2ban":
        '{log_type="security", source="fail2ban", %(hosts)s}',
    # fail2ban's bans and lifts alone: whether an address is banned now
    # (the Security view, C6). ``]`` outside a class is a plain ``]``.
    "f2b_bans":
        '{log_type="security", source="fail2ban", %(hosts)s}'
        ' |~ "] (Restore )?(Ban|Unban) "',
    # Every sshd line of a server: "see lines" of the SSH tables.
    "sshd":
        '{log_type="security", source="sshd", %(hosts)s}',
    # The instances' failed web logins (C1.9).
    "weblogin":
        '{log_type="security", source="weblogin", %(hosts)s}',
    # Every line of a server: "see lines".
    "lines":
        '{log_type="security", %(hosts)s}',
}


def logql_lines(name, host_ids):
    """Return the LogQL of one of :data:`LOGQL_LINES` for some servers.

    :param str name: a key of :data:`LOGQL_LINES`.
    :param host_ids: ``cloud.host`` ids, at least one.
    :rtype: str
    :raises KeyError: for a name that is not one of them.
    :raises ValueError: without a host, or with an id that is not a
        positive integer.
    """
    template = LOGQL_LINES[name]
    ids = sorted({_positive_int(host_id) for host_id in host_ids or ()})
    if not ids:
        raise ValueError("A security log query needs at least one host.")
    if len(ids) == 1:
        hosts = f'host_id="{ids[0]}"'
    else:
        hosts = 'host_id=~"' + "|".join(str(i) for i in ids) + '"'
    return template % {"hosts": hosts}


def logql_weblogin(host_id, instance_id):
    """Return the LogQL of one instance's failed web logins on one server.

    The instance is not a label (C1.9): the line filter keeps the lines
    whose ``instance=`` is that id, spaces around it so ``4`` is not
    ``42``.

    :param int host_id: the ``cloud.host`` id.
    :param int instance_id: the ``cloud.instance`` id, or 0 for the
        logins the server could not tell an instance of.
    :rtype: str
    :raises ValueError: for an id that is not an integer, or a host id
        that is not positive.
    """
    if isinstance(instance_id, bool) or not isinstance(instance_id, int) \
            or instance_id < 0:
        raise ValueError(f"Not an instance id: {instance_id!r}")
    return (
        logql_lines("weblogin", [host_id])
        + f' |= " instance={instance_id} "'
    )


#: A user name the sudo query may leave out in LogQL: written into the
#: expression, so nothing a regular expression or a LogQL string reads
#: as special.
_LOGQL_SAFE_USER = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def logql_sudo(host_ids, panel_user=None):
    """Return the LogQL of the sudo commands, the panel's user's left out.

    The panel's own sudo is most of what a server sends while a job
    runs (plan #84, A3): it is left out in Loki, as ``classify.lua``
    counts only the user, so it never fills the pages before another
    user's command. *host_ids* are servers the panel signs in to as
    *panel_user*. A user that cannot be written safely into LogQL is not
    left out here; the caller skips its lines.

    :param host_ids: ``cloud.host`` ids, at least one.
    :param str panel_user: the user the panel signs in as on them.
    :rtype: str
    """
    expression = logql_lines("sudo", host_ids)
    if panel_user and _LOGQL_SAFE_USER.match(panel_user):
        expression += rf' !~ "^\\s*{panel_user} : "'
    return expression


#: The sources the cron asks whether a server sends: the web logins can
#: be hundreds a minute, and say nothing of whether the rest arrives.
_SPEAKING = (
    'sum by (host_id) (count_over_time({log_type="security", '
    'source=~"sshd|sudo|fail2ban|auditd", %(hosts)s}[%(minutes)dm]))'
)


def logql_speaking(host_ids, minutes):
    """Return the LogQL that counts each server's lines of the last minutes.

    One instant query for every server: what tells the cron which ones
    came back from a silence (plan #84, M1).

    :param host_ids: ``cloud.host`` ids, at least one.
    :param int minutes: the window.
    :rtype: str
    """
    selector = logql_lines("lines", host_ids)
    hosts = selector[selector.index("host_id"):-1]
    return _SPEAKING % {"hosts": hosts, "minutes": int(minutes)}


def _positive_int(value):
    """Return *value* as a positive int, or raise ValueError.

    :rtype: int
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Not a host id: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Sanitizing what the attacker writes
# ---------------------------------------------------------------------------

#: Longest user name a record carries.
USER_MAX = 32
#: Longest program a record carries.
PROGRAM_MAX = 64
#: Longest file name a record carries.
PATH_MAX = 128

#: The characters a user name keeps; any other becomes ``?``.
_NOT_USER = re.compile(r"[^A-Za-z0-9._-]")
#: The characters a program or a file name keeps; any other becomes ``?``.
_NOT_PATH = re.compile(r"[^A-Za-z0-9._/+-]")


def sanitize(value, limit, kind="user"):
    """Return *value* cut to *limit* and to a closed set of characters.

    What an attacker writes in a user name, a command or a file name
    reaches a message only through here: no control character, no
    Markdown or HTML, no line break, no length that buries the rest.
    A character outside the set becomes ``?``, so the reader sees that
    something was there.

    :param value: the text, or None.
    :param int limit: characters kept, at most.
    :param str kind: ``user`` (letters, digits, ``._-``) or ``path``
        (those and ``/+``).
    :rtype: str
    """
    pattern = _NOT_PATH if kind == "path" else _NOT_USER
    return pattern.sub("?", str(value or "")[:limit])


def _user(value):
    """Return a user name sanitized (:func:`sanitize`)."""
    return sanitize(value, USER_MAX)


def _program(value):
    """Return a program sanitized (:func:`sanitize`)."""
    return sanitize(value, PROGRAM_MAX, kind="path")


def _address(raw):
    """Return *raw* as a normalized IP address, or None.

    :rtype: str | None
    """
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _port(raw):
    """Return *raw* as a TCP port, or None.

    :rtype: int | None
    """
    port = int(raw)
    return port if 0 < port < 65536 else None


# ---------------------------------------------------------------------------
# sshd
# ---------------------------------------------------------------------------

#: ``Accepted <method> for <user> from <ip> port <n> ssh2[: <type> <fp>…]``.
#: A certificate adds its ID and its authority after the key's
#: fingerprint, which is the one that counts.
_SSH_ACCEPTED = re.compile(
    r"^Accepted (?P<method>[a-z][a-z/-]{0,40}) for (?:invalid user )?"
    r"(?P<user>\S+) from (?P<ip>\S+) port (?P<port>\d{1,5}) ssh2"
    r"(?:: (?P<key_type>[A-Z0-9-]{1,32}) (?P<fp>SHA256:[A-Za-z0-9+/]{43})"
    r"(?: .*)?)?$"
)

#: The line sshd ends a connection that never authenticated with: one per
#: failed attempt (F0, 9-nov-2026; classify.lua). The user is whatever
#: the client sent, spaces included, so the address is the last one on
#: the line: only sshd writes after it.
_SSH_FAILED = re.compile(
    r"^(?:Connection closed by|Connection reset by|Disconnected from"
    r"|Disconnecting) (?P<who>invalid|authenticating) user (?P<user>.*)"
    r" (?P<ip>[0-9A-Fa-f:.]+) port (?P<port>\d{1,5})(?:: [A-Za-z ]+)?"
    r" \[preauth\]\Z",
    re.DOTALL,
)


def parse_sshd(line):
    """Return the record of an sshd line, or None.

    ``{"kind": "accepted", "user", "ip", "port", "method", "key_type",
    "fingerprint"}`` for a login (``fingerprint`` None without a key),
    ``{"kind": "failed", "user", "ip", "port", "invalid"}`` for the end
    of a failed connection.

    :param str line: the line, as Loki holds it.
    :rtype: dict | None
    """
    match = _SSH_ACCEPTED.match(line or "")
    if match:
        ip = _address(match["ip"])
        port = _port(match["port"])
        if ip is None or port is None:
            return None
        return {
            "kind": "accepted",
            "user": _user(match["user"]),
            "ip": ip,
            "port": port,
            "method": match["method"],
            "key_type": match["key_type"],
            "fingerprint": match["fp"],
        }
    match = _SSH_FAILED.match(line or "")
    if match:
        ip = _address(match["ip"])
        port = _port(match["port"])
        if ip is None or port is None:
            return None
        return {
            "kind": "failed",
            "user": _user(match["user"]),
            "ip": ip,
            "port": port,
            "invalid": match["who"] == "invalid",
        }
    return None


# ---------------------------------------------------------------------------
# sudo
# ---------------------------------------------------------------------------

#: ``<user> : [<refusal> ; ][TTY=… ; ]PWD=… ; USER=<as> ; …COMMAND=<cmd>``.
#: A refusal (``1 incorrect password attempt``, ``user NOT in sudoers``)
#: comes before the fields and has neither ``;`` nor ``=``. sudo-rs
#: (Ubuntu 26.04's sudo) writes two spaces after the colon.
_SUDO = re.compile(
    r"^\s*(?P<user>\S+) : +(?:(?P<refusal>[^;=]+) ; )?"
    r"(?:TTY=\S+ ; )?PWD=.* ; USER=(?P<run_as>\S+) ; (?:.* ; )?"
    r"COMMAND=(?P<command>.*)\Z",
    re.DOTALL,
)


def parse_sudo(line):
    """Return the record of a sudo line, or None.

    ``{"kind": "command" | "refused", "user", "run_as", "program",
    "become", "ambiguous"}``. ``program`` is the command's first word,
    without its arguments, which can carry secrets. ``become`` is
    Ansible's mark, which anyone can type: it means something only for
    the panel's own user. A line where ``USER=`` or ``COMMAND=`` shows up
    twice (a directory or an argument that writes them) cannot be split
    for sure: it is ``ambiguous``, with no ``run_as`` and no
    ``program``.

    :param str line: the line, as Loki holds it.
    :rtype: dict | None
    """
    match = _SUDO.match(line or "")
    if not match:
        return None
    ambiguous = line.count(" ; COMMAND=") > 1 or line.count(" ; USER=") > 1
    command = match["command"]
    words = command.split(None, 1)
    return {
        "kind": "refused" if match["refusal"] else "command",
        "user": _user(match["user"]),
        "run_as": None if ambiguous else _user(match["run_as"]),
        "program": None if ambiguous else _program(words[0] if words else ""),
        "become": "BECOME-SUCCESS-" in command,
        "ambiguous": ambiguous,
    }


# ---------------------------------------------------------------------------
# fail2ban
# ---------------------------------------------------------------------------

_F2B = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} fail2ban\.[a-z.]+ +\[\d+\]: "
    r"[A-Z]+ +\[(?P<jail>[A-Za-z0-9_.-]{1,32})\] (?P<rest>.*)$"
)
_F2B_ACTION = re.compile(
    r"^(?P<action>Found|Ban|Unban|Restore Ban) (?P<ip>\S+)(?: - .*)?$"
)
_F2B_ALREADY = re.compile(r"^(?P<ip>\S+) already banned$")
#: The action names the counters use (classify.lua).
_F2B_ACTIONS = {
    "Found": "found", "Ban": "ban", "Unban": "unban", "Restore Ban": "ban",
}


def parse_fail2ban(line):
    """Return the record of a fail2ban line, or None.

    ``{"jail", "action", "ip"}``, ``action`` one of ``found``, ``ban``,
    ``unban`` or ``already_banned``. The daemon's own lines (start, stop,
    configuration) are None.

    :param str line: the line, as Loki holds it.
    :rtype: dict | None
    """
    match = _F2B.match(line or "")
    if not match:
        return None
    rest = match["rest"]
    found = _F2B_ACTION.match(rest)
    if found:
        action = _F2B_ACTIONS[found["action"]]
    else:
        found = _F2B_ALREADY.match(rest)
        if not found:
            return None
        action = "already_banned"
    ip = _address(found["ip"])
    if ip is None:
        return None
    return {"jail": match["jail"], "action": action, "ip": ip}


# ---------------------------------------------------------------------------
# The instances' failed web logins
# ---------------------------------------------------------------------------

#: ``weblogin failed ip=<ip> instance=<id> t=<time>``, the line the
#: server's classification writes itself (C1.9), the whole line: nothing
#: before it and nothing after.
_WEBLOGIN = re.compile(
    r"^weblogin failed ip=(?P<ip>[0-9A-Fa-f:.]{2,45})"
    r" instance=(?P<instance>\d{1,12})"
    r" t=(?P<time>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?"
    r"(?:Z|[+-]\d\d:\d\d))\Z"
)


def parse_weblogin(line):
    """Return the record of a failed web login line, or None.

    ``{"ip", "instance_id", "time"}``: the address as ``ipaddress``
    writes it, the instance's id (0 when the server could not tell
    which), and the proxy's time as it wrote it (RFC 3339). Anything
    else on the line, an address ``ipaddress`` refuses or an id that is
    not a number, and the line is none.

    :param str line: the line, as Loki holds it.
    :rtype: dict | None
    """
    match = _WEBLOGIN.match(line or "")
    if not match:
        return None
    ip = _address(match["ip"])
    if ip is None:
        return None
    return {
        "ip": ip,
        "instance_id": int(match["instance"]),
        "time": match["time"],
    }


def source_key(address):
    """Return what tells two attackers apart: the address, IPv6 by its /64.

    Whoever holds an IPv6 /64 can take a new address of it for every
    attempt; counted by address, a single attacker would look like
    many with one attempt each.

    :param str address: an address ``ipaddress`` reads.
    :returns: the address, or its /64 written ``2001:db8:1:2::/64``.
    :rtype: str
    """
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


# ---------------------------------------------------------------------------
# auditd
# ---------------------------------------------------------------------------

#: ``type=SYSCALL|PATH msg=audit(<seconds>:<serial>): <fields>``; with
#: ``log_format = ENRICHED`` the translated fields follow a 0x1D, and only
#: the raw ones before it are read.
_AUDIT = re.compile(
    r"^type=(?P<type>SYSCALL|PATH) "
    r"msg=audit\((?P<stamp>\d{1,12}\.\d{3}:\d{1,20})\): (?P<fields>[^\x1d]*)"
)
_HEX = re.compile(r"^(?:[0-9A-F]{2})+$")
#: auditd's unset login uid.
AUID_UNSET = 4294967295


def _audit_fields(text):
    """Return the ``name=value`` fields of a record, the first of each.

    auditd writes a string with a space, a quote or a control character
    in hex, never quoted, so no value holds a space.

    :rtype: dict
    """
    fields = {}
    for word in text.split(" "):
        name, sep, value = word.partition("=")
        if sep and name not in fields:
            fields[name] = value
    return fields


def _audit_text(value):
    """Return an auditd string field decoded: quoted, hex or ``(null)``.

    :rtype: str
    """
    if value is None or value == "(null)":
        return ""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    if _HEX.match(value):
        return bytes.fromhex(value).decode("utf-8", "replace")
    return value


def _audit_int(value):
    """Return an auditd number, or None.

    :rtype: int | None
    """
    return int(value) if value and value.isdigit() else None


def parse_auditd(line):
    """Return the record of an auditd line, or None.

    A ``SYSCALL``: ``{"type": "SYSCALL", "event", "time", "key", "auid",
    "uid", "pid", "ppid", "comm", "exe", "success"}``. A ``PATH``:
    ``{"type": "PATH", "event", "time", "item", "name", "nametype"}``.
    ``event`` is ``audit(<seconds>:<serial>)``, what joins them.

    :param str line: the line, as Loki holds it.
    :rtype: dict | None
    """
    match = _AUDIT.match(line or "")
    if not match:
        return None
    stamp = match["stamp"]
    fields = _audit_fields(match["fields"])
    record = {
        "type": match["type"],
        "event": f"audit({stamp})",
        "time": float(stamp.partition(":")[0]),
    }
    if match["type"] == "SYSCALL":
        record.update({
            "key": sanitize(_audit_text(fields.get("key")), 32),
            "auid": _audit_int(fields.get("auid")),
            "uid": _audit_int(fields.get("uid")),
            "pid": _audit_int(fields.get("pid")),
            "ppid": _audit_int(fields.get("ppid")),
            "comm": _program(_audit_text(fields.get("comm"))),
            "exe": _program(_audit_text(fields.get("exe"))),
            "success": fields.get("success") == "yes",
        })
        return record
    item = _audit_int(fields.get("item"))
    if item is None:
        return None
    record.update({
        "item": item,
        "name": sanitize(_audit_text(fields.get("name")), PATH_MAX, "path"),
        "nametype": sanitize(fields.get("nametype"), 16),
    })
    return record


class AuditJoin(NamedTuple):
    """What :func:`join_audit` made of the auditd lines.

    :ivar events: one dict per change, oldest first.
    :ivar deferred: the ``(host_id, event)`` of each ``SYSCALL`` left for
        the next pass.
    """

    events: list
    deferred: list


def join_audit(lines, window_end_ns, defer_seconds=AUDIT_DEFER_SECONDS):
    """Join the ``SYSCALL`` and ``PATH`` records of every page read.

    The identity of a change is its server and its ``audit(…)`` stamp:
    the same record read twice, also with two different Loki instants,
    is one change. A ``PATH`` with no ``SYSCALL`` is left out. A
    ``SYSCALL`` with no ``PATH``, read in the last *defer_seconds* of the
    window, is deferred: its ``PATH`` may not have reached Loki yet, and
    the next pass's overlap brings both. Older, it is a change whose
    file is not known (``file`` None).

    Each event: ``{"host_id", "event", "ns", "time", "key", "auid", "uid",
    "pid", "ppid", "comm", "exe", "success", "file", "paths"}``, where
    ``paths`` is ``[{"item", "name", "nametype"}]`` by item and ``file``
    the name of the first that is not the parent directory.

    :param lines: :class:`LogLine` of source auditd, all pages together.
    :param int window_end_ns: the end of the window read, nanoseconds.
    :param int defer_seconds: the margin before that end.
    :rtype: AuditJoin
    """
    calls = {}
    paths = {}
    for log_line in lines:
        record = parse_auditd(log_line.line)
        if record is None:
            continue
        identity = (log_line.host_id, record["event"])
        if record["type"] == "SYSCALL":
            calls.setdefault(identity, (log_line.ns, record))
        else:
            paths.setdefault(identity, {}).setdefault(record["item"], record)

    cutoff = int(window_end_ns) - int(defer_seconds) * 1_000_000_000
    events = []
    deferred = []
    for identity, (ns, record) in calls.items():
        items = paths.get(identity) or {}
        if not items and ns >= cutoff:
            deferred.append(identity)
            continue
        ordered = [
            {"item": item, "name": path["name"], "nametype": path["nametype"]}
            for item, path in sorted(items.items())
        ]
        named = [path for path in ordered if path["nametype"] != "PARENT"]
        chosen = (named or ordered or [None])[0]
        event = {
            key: value for key, value in record.items() if key != "type"
        }
        event.update({
            "host_id": identity[0],
            "ns": ns,
            "file": chosen["name"] if chosen else None,
            "paths": ordered,
        })
        events.append(event)
    events.sort(key=lambda event: (event["ns"], event["host_id"] or 0))
    return AuditJoin(events, deferred)


# ---------------------------------------------------------------------------
# The reader: the account's log route and its credential, together
# ---------------------------------------------------------------------------

#: Lines :func:`count_weblogin_sources` counts at most: the last
#: two pages of the window.
WEBLOGIN_SAMPLE_PAGES = 2

#: Addresses :func:`count_weblogin_sources` names.
WEBLOGIN_TOP = 3


class WebloginSources(NamedTuple):
    """The addresses with the most failed web logins on one instance.

    :ivar top: ``[(address, attempts)]``, most attempts first, at most
        :data:`WEBLOGIN_TOP`; an IPv6 address is its /64
        (:func:`source_key`).
    :ivar distinct: how many addresses the lines counted came from.
    :ivar attempts: how many lines were counted.
    :ivar cut: True when the window held more lines than were read, so
        the counts are of its last ones.
    """

    top: list
    distinct: int
    attempts: int
    cut: bool


def read_last_lines(
    base_url, expression, start_ns, end_ns, pages=WEBLOGIN_SAMPLE_PAGES,
    limit=PAGE_LIMIT, timeout=QUERY_TIMEOUT, token=None, user=None,
    verify=True,
):
    """Read the last lines of a window, newest page first, by time.

    The window is read in tranches of :data:`TRANCHE_SECONDS` (the oldest
    up to a second longer), the newest first, each from its end back; a tranche read whole hands
    over to the one before it. Each page ends at the first instant of
    the one after it, included (Loki's window leaves its end out, so it
    ends one nanosecond later); a line read twice, the same server,
    instant and text, counts once. *pages* full pages and *pages* ×
    *limit* lines are of the whole window: past them, the newest lines
    are kept.

    :param str base_url: the log read root (:func:`loki_base`).
    :param str expression: LogQL selecting lines.
    :param int start_ns: first instant, nanoseconds, included.
    :param int end_ns: last instant, nanoseconds, left out.
    :param int pages: full pages at most.
    :param int limit: lines a page asks for.
    :returns: the lines, oldest first, and whether the window held more.
    :rtype: tuple[list[LogLine], bool]
    :raises requests.RequestException: as :func:`logql_query_range`.
    :raises ValueError: as :func:`logql_query_range`.
    """
    seen = set()
    lines = []
    wanted = pages * limit
    full = 0
    start = int(start_ns)
    upper = int(end_ns)
    tranche = TRANCHE_SECONDS * 1_000_000_000
    while upper > start:
        lower = upper - tranche
        if lower - start < 1_000_000_000:
            # The callers' window ends a nanosecond past its instant: a
            # remainder that small is the oldest tranche's, not one more.
            lower = start
        end = upper
        while True:
            page = logql_query_range(
                base_url, expression, lower, end, limit=limit,
                timeout=timeout, token=token, user=user, verify=verify,
                direction="backward",
            )
            fresh = [item for item in page if item[:3] not in seen]
            for item in fresh:
                seen.add(item[:3])
            lines.extend(fresh)
            if len(page) < limit:
                break
            full += 1
            if not fresh or full >= pages or len(lines) >= wanted:
                # The sample is taken, or more lines at one instant than
                # a page holds: what is left is older than it takes.
                return _read(lines, complete=False).lines[-wanted:], True
            end = page[0].ns + 1
        if len(lines) >= wanted:
            # Read whole down to here; older tranches are left unread.
            cut = len(lines) > wanted or lower > start
            return _read(lines, complete=not cut).lines[-wanted:], cut
        upper = lower
    return _read(lines, complete=True).lines, False


class LogReader:
    """The account's log read route on the central, with its credential.

    What reads the security logs from a model: built from the settings
    by :meth:`from_env`, so a caller never handles the URL or the
    credential, and a test hands a ``MagicMock(spec=LogReader)`` in its
    place.
    """

    def __init__(self, base_url, token=None, user=None, verify=True):
        """Keep where and as whom to read.

        :param str base_url: the log read root (:func:`loki_base`).
        :param token: password half of the account's credential.
        :param user: its user.
        :param verify: the central's certificate check.
        """
        self.base_url = base_url
        self.token = token
        self.user = user
        self.verify = verify

    @classmethod
    def from_env(cls, env):
        """Return the reader of this panel's central, or None.

        None when observability is off, without a central, or with one
        that is not a central this panel's version deploys (no ``/r``
        to put ``/lr`` beside).

        :param env: an Odoo environment.
        :rtype: LogReader | None
        """
        settings = env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return None
        base = loki_base(settings.metrics_central_url)
        if not base:
            return None
        user, token = settings._metrics_auth()
        return cls(base, token=token, user=user,
                   verify=settings._central_tls_verify())

    def read_lines(self, expression, start_ns, end_ns, max_pages=MAX_PAGES,
                   limit=PAGE_LIMIT):
        """Read a window page after page: :func:`read_lines`.

        :rtype: LogRead
        """
        return read_lines(
            self.base_url, expression, start_ns, end_ns, max_pages=max_pages,
            limit=limit, token=self.token, user=self.user, verify=self.verify,
        )

    def query(self, expression, at_ns=None):
        """Run a LogQL metric query at one instant: :func:`logql_query`.

        :rtype: list[tuple[dict, float]]
        """
        return logql_query(
            self.base_url, expression, at_ns=at_ns, token=self.token,
            user=self.user, verify=self.verify,
        )

    def weblogin_sources(self, host_id, instance_id, minutes=10, now_ns=None):
        """Return the addresses with the most failed web logins, or None.

        :func:`count_weblogin_sources` on this reader's central.

        :rtype: WebloginSources | None
        """
        return count_weblogin_sources(
            self.base_url, host_id, instance_id, minutes=minutes,
            now_ns=now_ns, token=self.token, user=self.user,
            verify=self.verify,
        )


def count_weblogin_sources(
    base_url, host_id, instance_id, minutes=10, now_ns=None, token=None,
    user=None, verify=True,
):
    """Return the addresses with the most failed web logins, or None.

    Reads lines, not an aggregation: the last two pages (1 000 lines) of
    the instance's failed logins on that server in the last *minutes*,
    counted by address on the panel (IPv6 by its /64). So no series per
    address is made anywhere and Loki's ``max_query_series`` never
    refuses it, also under an attack from thousands of addresses.

    Never raises: a central that is busy, fails or does not answer gives
    None, "no data", and the alert that asked goes out without
    addresses.

    :param str base_url: the log read root (:func:`loki_base`).
    :param int host_id: the ``cloud.host`` id.
    :param int instance_id: the ``cloud.instance`` id.
    :param int minutes: the window, up to now.
    :param now_ns: the end of the window, nanoseconds; now when None.
    :param token: password half of the account's credential.
    :param user: its user.
    :param verify: the central's certificate check.
    :rtype: WebloginSources | None
    """
    end = int(now_ns if now_ns is not None else time.time_ns())
    start = end - int(minutes) * 60 * 1_000_000_000
    try:
        expression = logql_weblogin(host_id, instance_id)
        lines, cut = read_last_lines(
            base_url, expression, start, end + 1,
            pages=WEBLOGIN_SAMPLE_PAGES, limit=PAGE_LIMIT,
            token=token, user=user, verify=verify,
        )
    except Exception as exc:  # noqa: BLE001 — "no data", never a failure
        _logger.warning(
            "[logs] failed web logins of instance %s on host %s not "
            "read: %s", instance_id, host_id, exc,
        )
        return None
    counts = {}
    attempts = 0
    for log_line in lines:
        record = parse_weblogin(log_line.line)
        if record is None or record["instance_id"] != instance_id:
            continue
        attempts += 1
        key = source_key(record["ip"])
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return WebloginSources(top[:WEBLOGIN_TOP], len(counts), attempts, cut)


def weblogin_top_sources(
    base_url, host_id, instance_id, minutes=10, token=None, user=None,
    verify=True, now_ns=None,
):
    """Return the ``(address, count)`` pairs of an instance's failed web logins.

    What the web-login rule's alert names (C5): the addresses with the
    most attempts, most first, at most :data:`WEBLOGIN_TOP`, IPv6 as its
    /64 (``2001:db8:1:2::/64``), counted as :func:`count_weblogin_sources`
    does. Never raises.

    :param str base_url: ``metrics_central_url`` (its ``/r`` becomes
        ``/lr/loki``), or the log read root itself.
    :param int host_id: the ``cloud.host`` id.
    :param int instance_id: the ``cloud.instance`` id.
    :param int minutes: the window, up to now.
    :param token: password half of the account's credential.
    :param user: its user.
    :param verify: the central's certificate check.
    :param now_ns: the end of the window, nanoseconds; now when None.
    :returns: the pairs, or None with nothing to say: no attempt read, a
        central busy, failing or not one of ours.
    :rtype: list[tuple[str, int]] | None
    """
    root = (base_url or "").strip().rstrip("/")
    if not root.endswith(_LOG_READ_ROOT):
        root = loki_base(root)
    if not root:
        return None
    counted = count_weblogin_sources(
        root, host_id, instance_id, minutes=minutes, now_ns=now_ns,
        token=token, user=user, verify=verify,
    )
    if not counted or not counted.top:
        return None
    return list(counted.top)
