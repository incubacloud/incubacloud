"""The alerts of the servers' security events, read from Loki every 5 minutes.

Plan #84, cut C4, with Fable's review (A1, A3, A4, B1, M1, M2, M5, M6,
M7). The servers send their sshd, sudo and auditd lines to the central's
Loki (F3, C1); this cron reads them back with the account's credential
(``models/logs.py``) and raises an alert for what the panel did not do:

- an SSH sign-in with a key that is not the panel's, with a password on a
  server the panel signs in to with a key, or, on a server the panel
  signs in to with a password, as another user
  (``security_ssh_login_foreign``, critical);
- a sign-in with the panel's key from an address the panel does not
  connect from (``security_ssh_login_panel_key_elsewhere``, critical),
  only once a connection of the panel's own says the address is not its
  own: one connection per server and pass, which also teaches the panel
  its new address after a change; when it cannot connect, a warning,
  ``security_ssh_login_unconfirmed``;
- a change to the files auditd watches (passwd, shadow, sudoers, the SSH
  configuration) by a user that is not the panel's, or by the panel's
  user or root outside every job, console and terminal of the panel
  (``security_sensitive_change``);
- a sudo command by a user that is not the panel's
  (``security_sudo_foreign``, warning).

**Known keys.** A sign-in with a key an administrator added to the
server's known keys (``cloud_host_known_key.py``), within the key's
window (from its addition to its removal, against the line's instant,
the server's own clock), raises nothing and is not remembered as an
event: each pass that reads it again judges it again. The panel's own
key is looked at first, so a known key never covers it from elsewhere.
From outside the addresses a key may sign in from, it is
``security_ssh_login_foreign`` (critical). Its sudo and its changes to
the watched files are judged as everyone's: the lines carry no session
to tie them to the sign-in.

**Three queries, each with its cursor.** Sign-ins, sudo and auditd are
read apart, so a deployment's sudo lines never fill the pages before a
sign-in, and the panel's own sudo is left out in Loki. Each window runs
from 15 minutes before the cursor to 30 seconds ago, at most 10 pages of
500 lines. The cursor starts at "now" (nothing earlier is alerted on),
does not move when the read fails, and moves only to the last line read
when the pages ran out: two passes in a row like that raise
``security_events_backlog``. After an outage of the central the window
reaches 24 hours back.

**Late servers.** A server whose lines could not leave it sends them
later with their own time, before the window. One instant query a pass
says which servers sent lines in the last 15 minutes; one that had been
silent longer is read again, alone, from where its silence began.

**Once.** ``cloud.security.event`` remembers every event that raised or
refreshed an alert, so the overlap never counts one twice and an alert a
user dismissed does not come back for the same event. The alerts do not
resolve themselves: a repeat while one is active refreshes its text with
the count.

**What reaches a message.** Only what ``logs.py`` sanitized (user,
program, file) and addresses read by ``ipaddress``; the address only in
the two sign-in alerts (decision D2), never a country; the payload keeps
the count and the last event's fields, never a line. The texts are
translated with ``env._`` in the language of the cron's context, as every
other producer of alerts; a translation whose placeholders are broken
gives the English text (Odoo's own fallback).
"""
import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models

from . import logs
from ._concurrency import is_concurrency_error, read_committed_cursor
from .cloud_host_known_key import match_known_key
from .cloud_host_observability import _panel_source_network
from .cloud_security_event import event_identity

_logger = logging.getLogger(__name__)

#: The three reads, each with its cursor.
QUERIES = ("accepted", "sudo", "auditd")

#: ``ir.config_parameter`` keys of each read's cursor (Loki nanoseconds)
#: and of how many passes in a row it stopped at its page limit.
CURSOR_PARAM = "incubacloud.security_events_cursor.%s"
BACKLOG_PARAM = "incubacloud.security_events_backlog.%s"

_SECOND_NS = 1_000_000_000

#: How far before its cursor a window starts: a line Loki took late is
#: still read.
OVERLAP_MINUTES = 15
OVERLAP_NS = OVERLAP_MINUTES * 60 * _SECOND_NS

#: How far before now a window ends: what Loki is still taking in.
SETTLE_NS = 30 * _SECOND_NS

#: How far back a window or a silent server's reading reaches at most.
LOOKBACK_NS = 24 * 3600 * _SECOND_NS

#: Margin around a job, a console or a terminal of the panel.
WINDOW_MARGIN = timedelta(seconds=60)

#: How long after a change of a server's key a sign-in with the key it
#: replaced, from before the change, is still the panel's (B1).
KEY_CHANGE_GRACE = timedelta(seconds=60)

#: Connections of the panel's own a pass opens at most to confirm an
#: address (A1); the sign-ins left waiting are read again the next pass.
PROBES_PER_PASS = 10

#: Servers back from a silence read again per pass, the longest silent
#: first.
RETURNS_PER_PASS = 5

#: Passes in a row stopped at the page limit before the backlog warning.
BACKLOG_AFTER = 2

#: The level of a change made as root outside every window of the panel.
#: A warning, provisional until step M-b measures root's changes on the
#: fleet: root's crontab writes with auid 0 too (plan #84, M7).
ROOT_CHANGE_LEVEL = "warning"

#: auditd's keys of the watched files (``host_hardening.yml``).
WATCHED_KEYS = frozenset({
    "passwd_changes", "shadow_changes", "sudoers_changes",
    "ssh_config_changes",
})

#: Job states in which a job of the panel may still be signed in.
_UNFINISHED_JOB_STATES = (
    "queued", "pending", "enqueued", "wait_dependencies", "started",
)

CODE_FOREIGN = "security_ssh_login_foreign"
CODE_ELSEWHERE = "security_ssh_login_panel_key_elsewhere"
CODE_UNCONFIRMED = "security_ssh_login_unconfirmed"
CODE_CHANGE = "security_sensitive_change"
CODE_SUDO = "security_sudo_foreign"
CODE_BACKLOG = "security_events_backlog"

#: The event alerts: raised once per event, never closed by themselves.
EVENT_CODES = (CODE_FOREIGN, CODE_ELSEWHERE, CODE_UNCONFIRMED, CODE_CHANGE, CODE_SUDO)

#: How often an open event alert's repeats reach the channels, per server.
#: Each new event notifies again while its alert is open, but one server
#: notifies once an hour at most, counting the alerts it opened: the
#: panel's own operators sign in to their servers over SSH, and every
#: sign-in would page them otherwise (plan #84, D8, decided 2026-11-14).
NOTICE_EVERY = timedelta(hours=1)

#: A sign-in left for the next pass: no connection left to confirm it.
HOLD = "hold"

#: A sign-in with one of the server's known keys, from where it may sign
#: in from: nobody's alert.
KNOWN = "known"


def _to_datetime(ns):
    """Return Loki's nanoseconds as a naive UTC datetime, as Odoo stores them.

    :param int ns: nanoseconds since the epoch.
    :rtype: datetime
    """
    return datetime.fromtimestamp(ns / _SECOND_NS, tz=timezone.utc).replace(
        tzinfo=None,
    )


def _audit_moment(event):
    """Return when an auditd change happened, as a naive UTC datetime.

    auditd's own ``audit(<seconds>:…)`` stamp, unless it is more than
    :data:`LOOKBACK_NS` away from the instant Loki holds the line at: a
    line written by hand can carry any number there, even one no
    datetime holds, and then Loki's instant is the one used.

    :param dict event: one of ``logs.join_audit``'s events.
    :rtype: datetime
    """
    ns = int(event["ns"])
    try:
        stamp = int(float(event["time"]) * _SECOND_NS)
    except (TypeError, ValueError, OverflowError):
        stamp = ns
    if abs(stamp - ns) > LOOKBACK_NS:
        stamp = ns
    return _to_datetime(stamp)


def _to_ns(moment):
    """Return a naive UTC datetime as Loki's nanoseconds.

    :param datetime moment: as Odoo stores it.
    :rtype: int
    """
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * _SECOND_NS)


def _clock(moment):
    """Return *moment* as the alerts write it: date, minute and UTC.

    :param datetime moment: naive UTC.
    :rtype: str
    """
    return moment.strftime("%Y-%m-%d %H:%M UTC")


class PanelSourceProbe:
    """One connection of the panel's own to a server: where does it come from?

    What confirms an address before the critical of the panel's key used
    from elsewhere (plan #84, A1). The connection is the panel's, like
    any job's, and the line it leaves in sshd's log is the panel's from
    the address it reads.
    """

    #: Seconds the whole connection may take.
    timeout = 45

    def read(self, host):
        """Return the address *host* sees the panel connect from.

        The first word of ``SSH_CONNECTION``, read without sudo, which
        clears it.

        :param host: the ``cloud.host``.
        :rtype: str
        :raises Exception: when the server cannot be reached or does not
            answer with an address.
        """

        async def _run():
            async with host.get_transport() as transport:
                return await transport.run("printenv SSH_CONNECTION", timeout=20)

        result = asyncio.run(asyncio.wait_for(_run(), self.timeout))
        address = (result.stdout or "").strip().split(" ")[0]
        if _panel_source_network(address) is None:
            raise ValueError("SSH_CONNECTION named no address")
        return address


class _Pass:
    """What one pass of the cron learns as it goes and asks only once.

    :ivar hosts: the servers read, by id.
    :ivar probe: what opens the confirming connections.
    :ivar probes_left: connections this pass may still open.
    :ivar probed: the address each server saw the panel connect from,
        None when the connection failed.
    :ivar confirmed: the networks a connection confirmed as the panel's.
    :ivar known: identities of the events already remembered.
    :ivar span: the window being read, as naive UTC datetimes.
    """

    def __init__(self, hosts, probe):
        """Start a pass over *hosts*.

        :param hosts: ``cloud.host`` records.
        :param PanelSourceProbe probe: the connection opener.
        """
        self.hosts = {host.id: host for host in hosts}
        self.probe = probe
        self.probes_left = PROBES_PER_PASS
        self.probed = {}
        self.confirmed = set()
        self.known = set()
        self.span = None
        self._fingerprints = {}
        self._known = {}
        self._windows = {}
        self.used = {}

    def fingerprints(self, host):
        """Return the panel's key fingerprints for *host*, read once a pass.

        :rtype: list[str]
        """
        if host.id not in self._fingerprints:
            self._fingerprints[host.id] = host._panel_key_fingerprints()
        return self._fingerprints[host.id]

    def known_keys(self, host):
        """Return *host*'s known keys, removed ones included, read once a pass.

        :rtype: list[cloud_host_known_key.KnownKey]
        """
        if host.id not in self._known:
            self._known[host.id] = host._security_known_keys()
        return self._known[host.id]

    def windows(self, host, reader):
        """Return the panel's windows on *host* over the span, read once.

        :param reader: a callable ``(host, since, until)`` returning them.
        :rtype: list[tuple[datetime, datetime]]
        """
        key = (host.id, self.span)
        if key not in self._windows:
            self._windows[key] = reader(host, *self.span)
        return self._windows[key]


class CloudSecurityEvents(models.AbstractModel):
    _name = "cloud.security.events"
    _description = "Security events of the servers"

    # ── The cron ──────────────────────────────────────────────────────────

    @api.model
    def _cron_security_events(self):
        """Read the servers' new security events and raise their alerts.

        Every 5 minutes. Nothing without a central of this panel's own
        kind or without a server sending its security logs with the
        agents of revision 4 (they carry ``host_id``). Each read fails on
        its own: a central that is down or busy leaves its cursor where it
        was, for the next pass to read the same window again.
        """
        reader = self._security_reader()
        hosts = self._security_hosts() if reader is not None else None
        if not hosts:
            # Nothing is read, so no read is behind (Fable's final review
            # of F4, B3).
            Alert = self.env["cloud.alert"].sudo()
            for query in QUERIES:
                Alert.resolve_alert(CODE_BACKLOG, key=query)
            return
        end = self._security_now_ns() - SETTLE_NS
        run = _Pass(hosts, self._security_probe())
        self._security_returns(reader, run, end)
        for query in QUERIES:
            self._security_pass(reader, run, query, end)

    @api.model
    def _security_reader(self):
        """Return the log reader of this panel's central, or None.

        :rtype: logs.LogReader | None
        """
        return logs.LogReader.from_env(self.env)

    @api.model
    def _security_probe(self):
        """Return what opens the panel's confirming connections.

        :rtype: PanelSourceProbe
        """
        return PanelSourceProbe()

    @api.model
    def _security_now_ns(self):
        """Return now, in Loki's nanoseconds.

        :rtype: int
        """
        return time.time_ns()

    @api.model
    def _security_hosts(self):
        """Return the servers whose security events are read.

        Those sending their security logs whose agents were installed
        with C1 (``metrics_write_user`` is written from then on, and the
        lines carry ``host_id``), and none where this panel may not
        receive security logs.

        :rtype: cloud.host
        """
        Host = self.env["cloud.host"].sudo()
        if not Host._security_logs_available():
            return Host.browse()
        return Host.search([
            ("security_logs_enabled", "=", True),
            ("metrics_agents_state", "=", "installed"),
            ("metrics_write_user", "!=", False),
        ])

    # ── One read, its cursor and its backlog ──────────────────────────────

    def _security_pass(self, reader, run, query, end):
        """Read one of the three windows, raise its alerts, move its cursor.

        :param logs.LogReader reader: the central's log reader.
        :param _Pass run: this pass.
        :param str query: one of :data:`QUERIES`.
        :param int end: the window's end, nanoseconds.
        """
        params = self.env["ir.config_parameter"].sudo()
        cursor = _int_param(params.get_param(CURSOR_PARAM % query))
        if cursor is None:
            # Installed now, or the parameter lost: nothing before now is
            # alerted on (plan #84, M1).
            params.set_param(CURSOR_PARAM % query, str(end))
            return
        backlog = _int_param(params.get_param(BACKLOG_PARAM % query)) or 0
        # Behind on a backlog, the window starts at the last line read:
        # going back 15 minutes would read the same full pages again.
        start = cursor if backlog else cursor - OVERLAP_NS
        start = max(start, end - LOOKBACK_NS)
        if start >= end:
            return
        hosts = self.env["cloud.host"].sudo().browse(list(run.hosts))
        try:
            read = self._security_read(reader, query, hosts, start, end)
        except Exception as exc:  # noqa: BLE001 — the cursor stays: read again next pass
            _logger.warning(
                "[security] %s not read; its cursor stays: %s", query, exc,
            )
            return
        hold = self._security_handle(run, query, read, start, end)
        moved = end if read.complete else read.last_ns
        if hold is not None:
            moved = min(moved, hold)
        params.set_param(CURSOR_PARAM % query, str(max(moved, start)))
        self._security_backlog(query, read, backlog)

    def _security_read(self, reader, query, hosts, start, end):
        """Read one window of *query* for *hosts*.

        sudo is read once per user the panel signs in as, each read
        leaving that user out in Loki: the user is the panel's only on
        the servers it signs in to as that user.

        :param logs.LogReader reader: the central's log reader.
        :param str query: one of :data:`QUERIES`.
        :param hosts: ``cloud.host`` records.
        :param int start: first instant, nanoseconds.
        :param int end: last instant, nanoseconds.
        :rtype: logs.LogRead
        """
        if query != "sudo":
            return reader.read_lines(logs.logql_lines(query, hosts.ids), start, end)
        groups = {}
        for host in hosts:
            groups.setdefault(host.user or "", []).append(host.id)
        reads = [
            reader.read_lines(logs.logql_sudo(ids, user), start, end)
            for user, ids in sorted(groups.items())
        ]
        lines = sorted(
            (line for read in reads for line in read.lines),
            key=lambda item: (item.ns, item.host_id or 0, item.line),
        )
        complete = all(read.complete for read in reads)
        stops = [read.last_ns for read in reads if not read.complete]
        last = min(stops) if stops else (lines[-1].ns if lines else None)
        return logs.LogRead(lines, last, complete)

    def _security_backlog(self, query, read, backlog):
        """Count the passes in a row a read stopped at its page limit.

        At :data:`BACKLOG_AFTER`, the warning that the alerts come late;
        resolved by the first pass that reads its window to the end.

        :param str query: one of :data:`QUERIES`.
        :param logs.LogRead read: what the pass read.
        :param int backlog: the passes in a row before this one.
        """
        params = self.env["ir.config_parameter"].sudo()
        Alert = self.env["cloud.alert"].sudo()
        if read.complete:
            if backlog:
                params.set_param(BACKLOG_PARAM % query, "0")
            Alert.resolve_alert(CODE_BACKLOG, key=query)
            return
        backlog += 1
        params.set_param(BACKLOG_PARAM % query, str(backlog))
        if backlog >= BACKLOG_AFTER:
            Alert.raise_alert(
                CODE_BACKLOG, self._security_backlog_text(query),
                level="warning", key=query,
                confirm_after=1, confirm_window=3600,
            )

    # ── Servers back from a silence (M1) ──────────────────────────────────

    def _security_returns(self, reader, run, end):
        """Read again the servers that come back from a silence.

        One instant query says which servers sent lines in the last 15
        minutes. One whose last line seen is older than that sent
        nothing for a while, and Fluent Bit sends what it kept with the
        time it happened, before the window: that server is read again
        alone, from its last line seen less the overlap, 24 hours at
        most. A server never seen before is only noted.

        :param logs.LogReader reader: the central's log reader.
        :param _Pass run: this pass.
        :param int end: the window's end, nanoseconds.
        """
        Host = self.env["cloud.host"].sudo()
        try:
            samples = reader.query(
                logs.logql_speaking(list(run.hosts), OVERLAP_MINUTES), at_ns=end,
            )
        except Exception as exc:  # noqa: BLE001 — the next pass asks again
            _logger.warning("[security] servers sending not read: %s", exc)
            return
        speaking = set()
        for labels, value in samples:
            raw = str((labels or {}).get("host_id") or "")
            if value > 0 and raw.isdigit() and int(raw) in run.hosts:
                speaking.add(int(raw))
        # Seconds, as the field keeps it.
        now = _to_datetime(end).replace(microsecond=0)
        silent_since = now - timedelta(minutes=OVERLAP_MINUTES)
        fresh = Host.browse()
        returning = []
        for host_id in sorted(speaking):
            host = run.hosts[host_id]
            last = host.security_last_line_at
            if last and last < silent_since:
                returning.append(host)
            else:
                fresh |= host
        if fresh:
            self._security_heard(fresh, now)
        returning.sort(key=lambda host: (host.security_last_line_at, host.id))
        for host in returning[:RETURNS_PER_PASS]:
            start = max(
                _to_ns(host.security_last_line_at) - OVERLAP_NS,
                end - LOOKBACK_NS,
            )
            if self._security_replay(reader, run, host, start, end):
                self._security_heard(host, now)

    def _security_heard(self, hosts, moment):
        """Write when *hosts* last sent lines, on a cursor of their own.

        These host rows are the ones a job's hook or a save of the server
        writes too: written in the cron's REPEATABLE READ snapshot, ahead
        of its reads of Loki and its connections, the second writer lost
        the row and the whole pass rolled back, with its alerts already
        sent (Fable's final review of F4, M1). The cron's own transaction
        never writes these rows.

        :param hosts: ``cloud.host`` records.
        :param datetime moment: naive UTC, in seconds.
        """
        with read_committed_cursor(self.env.registry) as cr:
            hosts.with_env(hosts.env(cr=cr)).sudo().write(
                {"security_last_line_at": moment},
            )
        hosts.invalidate_recordset(["security_last_line_at"])

    def _security_learn_source(self, host, address):
        """Record *address* as the panel's on *host*, on a cursor of its own.

        For the reason of :meth:`_security_heard`.

        :param host: the ``cloud.host``.
        :param str address: what a connection of the panel's own read.
        """
        with read_committed_cursor(self.env.registry) as cr:
            host.with_env(host.env(cr=cr)).sudo()._record_panel_source(address)
        host.invalidate_recordset(["security_panel_sources"])

    def _security_replay(self, reader, run, host, start, end):
        """Read the three windows again for one server.

        :param logs.LogReader reader: the central's log reader.
        :param _Pass run: this pass.
        :param host: the ``cloud.host``.
        :param int start: first instant, nanoseconds.
        :param int end: last instant, nanoseconds.
        :return: whether every read succeeded.
        :rtype: bool
        """
        _logger.info(
            "[security] cloud.host(id=%s) is back from a silence: read again "
            "from %s", host.id, _to_datetime(start),
        )
        for query in QUERIES:
            try:
                read = self._security_read(reader, query, host, start, end)
            except Exception as exc:  # noqa: BLE001 — tried again next pass
                _logger.warning(
                    "[security] %s of cloud.host(id=%s) not read again: %s",
                    query, host.id, exc,
                )
                return False
            if not read.complete:
                _logger.warning(
                    "[security] %s of cloud.host(id=%s) read again up to %s "
                    "only: more lines than a read takes", query, host.id,
                    _to_datetime(read.last_ns),
                )
            self._security_handle(run, query, read, start, end)
        return True

    # ── What the lines say ────────────────────────────────────────────────

    def _security_handle(self, run, query, read, start, end):
        """Raise the alerts of the lines one read gave.

        :param _Pass run: this pass.
        :param str query: one of :data:`QUERIES`.
        :param logs.LogRead read: what was read.
        :param int start: the window's first instant, nanoseconds.
        :param int end: the window's end, nanoseconds.
        :return: the instant the cursor must not pass, or None: a sign-in
            left for the next pass, or a change whose file has not arrived.
        :rtype: int | None
        """
        run.span = (_to_datetime(start) - WINDOW_MARGIN, _to_datetime(end))
        Event = self.env["cloud.security.event"]
        if query == "auditd":
            window_end = end if read.complete else read.last_ns
            joined = logs.join_audit(read.lines, window_end)
            events = [
                (run.hosts.get(event["host_id"]), event) for event in joined.events
            ]
            events = [(host, event) for host, event in events if host]
            run.known = Event._known(
                event_identity(host.id, event["ns"], audit_event=event["event"])
                for host, event in events
            )
            for host, event in events:
                self._security_isolated(host, self._security_change, run, host, event)
            if joined.deferred:
                return window_end - logs.AUDIT_DEFER_SECONDS * _SECOND_NS
            return None
        lines = [line for line in read.lines if line.host_id in run.hosts]
        run.known = Event._known(
            event_identity(line.host_id, line.ns, line=line.line) for line in lines
        )
        hold = None
        for line in lines:
            host = run.hosts[line.host_id]
            judge = self._security_login if query == "accepted" else self._security_sudo
            verdict = self._security_isolated(host, judge, run, host, line)
            if verdict == HOLD:
                hold = line.ns if hold is None else min(hold, line.ns)
        if query == "accepted":
            self._security_note_used(run)
        return hold

    def _security_isolated(self, host, judge, *args):
        """Judge one event in a savepoint of its own; log and skip what fails.

        A line that breaks its judgement (one written by hand on the
        server, say) neither stops the pass for every server nor rolls
        back the alerts the pass already raised and sent, which the next
        pass would raise and send again (Fable's final review of F4, M2).
        A lost race is raised still, for Odoo to run the cron again.

        :param host: the ``cloud.host`` the event came from.
        :param judge: the method that judges it.
        :param args: its arguments.
        :return: what *judge* returned, or None when it failed.
        """
        try:
            with self.env.cr.savepoint():
                return judge(*args)
        except Exception as exc:
            if is_concurrency_error(exc):
                raise
            _logger.exception(
                "[security] an event of cloud.host(id=%s) skipped: %s",
                host.id, exc,
            )
            return None

    def _security_login(self, run, host, line):
        """Judge one ``Accepted`` line and raise its alert, if any.

        :param _Pass run: this pass.
        :param host: the ``cloud.host`` it came from.
        :param logs.LogLine line: the line.
        :return: :data:`HOLD` when it waits for the next pass.
        """
        record = logs.parse_sshd(line.line)
        if not record or record["kind"] != "accepted":
            return None
        identity = event_identity(host.id, line.ns, line=line.line)
        if identity in run.known:
            return None
        verdict = self._security_login_verdict(run, host, record, line.ns)
        if verdict is None or verdict == HOLD:
            return verdict
        code, level, reason = verdict[:3]
        at = _to_datetime(line.ns)
        detail = {
            "user": record["user"], "ip": record["ip"],
            "method": logs.sanitize(record["method"], 32), "reason": reason,
            "time": fields.Datetime.to_string(at),
        }
        if len(verdict) > 3:
            detail["key_label"] = verdict[3]
        if code == CODE_UNCONFIRMED:
            # Only the two sign-in alerts carry the address (D2).
            del detail["ip"]
        self._security_raise(host, code, level, identity, at, detail)
        return None

    def _security_login_verdict(self, run, host, record, ns):
        """Return what a sign-in is: None (the panel's), HOLD or an alert.

        The table of plan #84, C4 (A1, A4, B1):

        - on a server the panel signs in to with a key, a key that is not
          its own (nor the one it replaced, read from before the change)
          or a password: someone else's;
        - the panel's key from the panel's address: its own; from
          another, a connection of the panel's own decides;
        - on a server the panel signs in to with a password (or a key it
          cannot read), by user and address: another user is someone
          else's; the panel's user from another address, the connection
          decides;
        - a key that is not the panel's but one of the server's known
          keys in its window: nobody's alert, from the addresses it may
          sign in from; from another, someone else's.

        :param _Pass run: this pass.
        :param host: the ``cloud.host``.
        :param dict record: what ``logs.parse_sshd`` gave.
        :param int ns: the line's instant.
        :return: None, :data:`HOLD` or ``(code, level, reason[, label])``.
        """
        fingerprints = run.fingerprints(host)
        if fingerprints:
            if not record["fingerprint"]:
                return (CODE_FOREIGN, "critical", "password")
            if record["fingerprint"] not in fingerprints and not (
                self._security_previous_key(host, record["fingerprint"], ns)
            ):
                known = self._security_known_key(run, host, record, ns)
                if known is None:
                    return (CODE_FOREIGN, "critical", "key")
                return None if known == KNOWN else known
            mismatch = (CODE_ELSEWHERE, "critical", None)
        else:
            known = self._security_known_key(run, host, record, ns)
            if known is not None:
                return None if known == KNOWN else known
            if record["user"] != logs.sanitize(host.user, logs.USER_MAX):
                return (CODE_FOREIGN, "critical", "user")
            mismatch = (CODE_FOREIGN, "critical", "address")
        if self._security_panel_address(host, record["ip"]):
            return None
        outcome = self._security_confirm(run, host, record["ip"])
        if outcome == "match":
            return None
        if outcome == HOLD:
            return HOLD
        if outcome == "mismatch":
            return mismatch
        return (CODE_UNCONFIRMED, "warning", None)

    def _security_known_key(self, run, host, record, ns):
        """Judge a sign-in by the server's known keys.

        :param _Pass run: this pass; a sign-in with a known key is noted
            in ``run.used`` for :meth:`_security_note_used`.
        :param host: the ``cloud.host``.
        :param dict record: what ``logs.parse_sshd`` gave.
        :param int ns: the line's instant, the server's clock.
        :return: None when no known key covers it, :data:`KNOWN` when one
            does from an address it may sign in from, or
            ``(CODE_FOREIGN, "critical", "known_key_address", label)``.
        """
        at = _to_datetime(ns)
        try:
            # A savepoint of its own: a database error reading the list
            # would otherwise leave the event's transaction aborted, and
            # its alert could not be written.
            with self.env.cr.savepoint():
                keys = run.known_keys(host)
            key = match_known_key(keys, record["fingerprint"], at)
        except Exception as exc:  # noqa: BLE001 — judged as if no key were known
            if is_concurrency_error(exc):
                raise
            # Fail closed: a list that cannot be read must not silence the
            # sign-in, which the event's own savepoint would skip.
            _logger.exception(
                "[security] the known keys of cloud.host(id=%s) not read: %s",
                host.id, exc,
            )
            return None
        if key is None:
            return None
        if not key.allows(record["ip"]):
            return (CODE_FOREIGN, "critical", "known_key_address", key.name)
        if key.removed_at is None:
            run.used[key.id] = max(at, run.used.get(key.id, at))
        return KNOWN

    def _security_note_used(self, run):
        """Write when each known key was last used, on a cursor of its own.

        One write per key and pass, never backwards; on a cursor of its
        own for the reason of :meth:`_security_heard`. A failure is logged
        and leaves the pass as it was.

        :param _Pass run: this pass.
        """
        if not run.used:
            return
        used, run.used = run.used, {}
        try:
            with read_committed_cursor(self.env.registry) as cr:
                Key = self.env["cloud.host.known.key"].with_env(
                    self.env(cr=cr),
                ).sudo().with_context(active_test=False)
                for key in Key.browse(list(used)).exists():
                    moment = used[key.id].replace(microsecond=0)
                    if not key.last_used_at or key.last_used_at < moment:
                        key.write({"last_used_at": moment})
        except Exception:  # noqa: BLE001 — the pass's alerts matter more than a date
            # A last use is a convenience: the pass and its alerts stand.
            _logger.exception("[security] the known keys' last use not written")
        self.env["cloud.host.known.key"].invalidate_model(["last_used_at"])

    def _security_previous_key(self, host, fingerprint, ns):
        """Return whether *fingerprint* was the panel's key at *ns*.

        The key a change replaced, for a sign-in from before the change
        (with a minute's margin): one with it afterwards is anyone's.

        :rtype: bool
        """
        host = host.sudo()
        previous = host.security_previous_key_fingerprint
        changed = host.security_key_changed_at
        return bool(
            previous and changed and fingerprint == previous
            and _to_datetime(ns) <= changed + KEY_CHANGE_GRACE
        )

    def _security_panel_address(self, host, address):
        """Return whether *address* is one the panel connects to *host* from.

        One the server saw the panel come from in the last 30 days
        (``security_panel_sources``), or the panel's own address when a
        layer knows it (``_hardening_panel_ip``, an address or a
        network); IPv6 by its /64.

        :rtype: bool
        """
        if host._panel_source_known(address):
            return True
        panel = (host._hardening_panel_ip() or "").strip()
        if not panel:
            return False
        try:
            network = ipaddress.ip_network(panel, strict=False)
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if ip.version != network.version:
            return False
        if network.num_addresses == 1:
            return _panel_source_network(address) == _panel_source_network(
                str(network.network_address),
            )
        return ip in network

    def _security_confirm(self, run, host, address):
        """Ask the server, with a connection of the panel's own, whose *address* is.

        One connection per server and pass (A1); what it reads is
        learned as the panel's address on that server, and an address it
        confirmed counts as the panel's on every server this pass,
        without another connection. With no connection left this pass,
        the sign-in waits for the next.

        :param _Pass run: this pass.
        :param host: the ``cloud.host``.
        :param str address: the sign-in's address.
        :return: ``match``, ``mismatch``, ``unreachable`` or :data:`HOLD`.
        :rtype: str
        """
        network = _panel_source_network(address)
        if network in run.confirmed:
            self._security_learn_source(host, address)
            return "match"
        if host.id not in run.probed:
            if run.probes_left <= 0:
                return HOLD
            run.probes_left -= 1
            try:
                seen = run.probe.read(host)
            except Exception as exc:  # noqa: BLE001 — "not confirmed", a warning
                _logger.warning(
                    "[security] cannot connect to cloud.host(id=%s) to confirm "
                    "the panel's address: %s", host.id, exc,
                )
                seen = None
            run.probed[host.id] = seen
            if seen:
                self._security_learn_source(host, seen)
                run.confirmed.add(_panel_source_network(seen))
        seen = run.probed[host.id]
        if seen is None:
            return "unreachable"
        if _panel_source_network(seen) == network:
            return "match"
        return "mismatch"

    def _security_sudo(self, run, host, line):
        """Judge one sudo line and raise its alert, if any.

        The panel's user's lines are mostly left out in Loki; one that
        could not be (a user name LogQL cannot hold safely) is skipped
        here. Any other user's is a warning, with the program and never
        its arguments.

        :param _Pass run: this pass.
        :param host: the ``cloud.host``.
        :param logs.LogLine line: the line.
        """
        record = logs.parse_sudo(line.line)
        if not record:
            return None
        if record["user"] == logs.sanitize(host.user, logs.USER_MAX):
            return None
        identity = event_identity(host.id, line.ns, line=line.line)
        if identity in run.known:
            return None
        at = _to_datetime(line.ns)
        self._security_raise(host, CODE_SUDO, "warning", identity, at, {
            "user": record["user"], "program": record["program"],
            "kind": record["kind"], "time": fields.Datetime.to_string(at),
        })
        return None

    def _security_change(self, run, host, event):
        """Judge one change to a watched file and raise its alert, if any.

        Who made it is told again on the panel from the login uid, with
        the uid the agents' install read for the panel's user (M2): a
        user that is not the panel's is critical; the panel's user
        outside every job, console and terminal of the panel, critical;
        root there, :data:`ROOT_CHANGE_LEVEL`; the system's own
        (no login uid), nothing.

        :param _Pass run: this pass.
        :param host: the ``cloud.host``.
        :param dict event: one of ``logs.join_audit``'s events.
        """
        if event.get("key") not in WATCHED_KEYS:
            return
        who = self._security_who(host, event.get("auid"))
        if who == "system":
            return
        identity = event_identity(host.id, event["ns"], audit_event=event["event"])
        if identity in run.known:
            return
        at = _audit_moment(event)
        if who == "user":
            level = "critical"
        elif self._security_in_window(run, host, at):
            return
        else:
            level = "critical" if who == "panel" else ROOT_CHANGE_LEVEL
        self._security_raise(host, CODE_CHANGE, level, identity, at, {
            "file": event.get("file"), "who": who,
            "auid": event.get("auid"),
            "program": event.get("exe") or event.get("comm") or "",
            "time": fields.Datetime.to_string(at),
        })

    def _security_who(self, host, auid):
        """Return who a login uid is on *host*: system, panel, root or user.

        Without the panel's uid (an install before it was recorded) a
        user cannot be told from the panel's: it is judged as the
        panel's, by its windows. Login uid 0 is root even where the panel
        signs in as root: root's scheduled tasks carry it too, so outside
        the panel's windows it takes :data:`ROOT_CHANGE_LEVEL`, as the
        alerts' reference says (Fable's final review of F4, B5).

        :param host: the ``cloud.host``.
        :param auid: auditd's login uid, or None.
        :rtype: str
        """
        if auid is None or auid == logs.AUID_UNSET:
            return "system"
        if auid == 0:
            return "root"
        uid = (host.security_panel_uid or "").strip()
        if uid and str(auid) == uid:
            return "panel"
        if not uid:
            return "panel"
        return "user"

    def _security_in_window(self, run, host, at):
        """Return whether the panel was at work on *host* at *at*.

        :param _Pass run: this pass.
        :param host: the ``cloud.host``.
        :param datetime at: naive UTC.
        :rtype: bool
        """
        return any(
            begin <= at <= finish
            for begin, finish in run.windows(host, self._security_windows)
        )

    @api.model
    def _security_windows(self, host, since, until):
        """Return when the panel was at work on *host* between two moments.

        Its jobs on the server or on its instances, its consoles and its
        instances' terminals, each with :data:`WINDOW_MARGIN` around it;
        one still running ends now. Read once per server and pass.

        :param host: the ``cloud.host``.
        :param datetime since: naive UTC.
        :param datetime until: naive UTC.
        :rtype: list[tuple[datetime, datetime]]
        """
        now = fields.Datetime.now()
        early = since - WINDOW_MARGIN
        late = until + WINDOW_MARGIN
        windows = []
        jobs = self.env["cloud.job"].sudo().search([
            ("create_date", "<=", late),
            "|", ("host_id", "=", host.id), ("instance_id.host_id", "=", host.id),
            "|", "|", ("state", "in", _UNFINISHED_JOB_STATES),
            ("date_done", ">=", early),
            "&", ("date_done", "=", False), ("write_date", ">=", early),
        ])
        for job in jobs:
            if job.state in _UNFINISHED_JOB_STATES:
                finish = now
            else:
                finish = job.date_done or job.write_date
            windows.append((job.create_date - WINDOW_MARGIN, finish + WINDOW_MARGIN))
        for model, field in (
            ("cloud.host.session", "host_id"),
            ("cloud.instance.session", "instance_id.host_id"),
        ):
            sessions = self.env[model].sudo().search([
                (field, "=", host.id),
                ("opened_at", "<=", late),
                "|", ("closed_at", "=", False), ("closed_at", ">=", early),
            ])
            for session in sessions:
                windows.append((
                    session.opened_at - WINDOW_MARGIN,
                    (session.closed_at or now) + WINDOW_MARGIN,
                ))
        return windows

    # ── The alerts ────────────────────────────────────────────────────────

    def _security_raise(self, host, code, level, identity, at, detail):
        """Raise or refresh *code* on *host* for one event, once.

        The event is remembered first: one read again, or one already
        counted by another pass, changes nothing. The alert keeps the
        worst level it had, counts its events since the first and says
        the last; its payload holds those fields alone, never a line,
        and when it last reached the channels (``notified``). A new
        event on an open alert reaches them again once the server's
        event alerts have been quiet for :data:`NOTICE_EVERY`; within
        it, it refreshes the alert alone. A new alert and one turning
        critical notify as every alert does.

        :param host: the ``cloud.host``.
        :param str code: the alert's code.
        :param str level: ``warning`` or ``critical``.
        :param str identity: :func:`event_identity`.
        :param datetime at: when it happened, naive UTC.
        :param dict detail: the event's sanitized fields.
        """
        if not self.env["cloud.security.event"]._remember(host, identity, code, at):
            return
        Alert = self.env["cloud.alert"].sudo()
        existing = Alert.search(Alert._dedup_domain(code, host=host), limit=1)
        before = existing.payload if existing and isinstance(existing.payload, dict) else {}
        count = int(before.get("count") or 0) + 1 if existing else 1
        since = at
        if existing and before.get("since"):
            try:
                since = min(at, fields.Datetime.to_datetime(before["since"]))
            except (TypeError, ValueError):
                since = at
        if existing.level == "critical":
            level = "critical"
        now = _to_datetime(self._security_now_ns())
        # raise_alert notifies a new alert and one turning critical.
        notifies = not existing or (existing.level != "critical" and level == "critical")
        notice = not notifies and self._security_quiet(host, now)
        if notifies or notice:
            notified = fields.Datetime.to_string(now)
        else:
            notified = before.get("notified") or fields.Datetime.to_string(
                existing.create_date,
            )
        alert = Alert.raise_alert(
            code, self._security_text(code, host, detail, count, since, at),
            level=level, host=host,
            payload={
                "count": count, "since": fields.Datetime.to_string(since),
                "last": detail, "notified": notified,
            },
            extra={"occurrences": count},
            confirm_after=1, confirm_window=3600,
        )
        if notice and alert:
            alert._dispatch_notifications()

    def _security_quiet(self, host, now):
        """Return whether *host*'s open event alerts notified an hour ago or more.

        Read from the alerts themselves, so it holds across restarts and
        writes no row of the server: an alert from before ``notified``
        was kept counts from its creation.

        :param host: the ``cloud.host``.
        :param datetime now: this pass's moment, naive UTC.
        :rtype: bool
        """
        alerts = self.env["cloud.alert"].sudo().search([
            ("host_id", "=", host.id), ("instance_id", "=", False),
            ("code", "in", EVENT_CODES), ("state", "=", "active"),
        ])
        for alert in alerts:
            payload = alert.payload if isinstance(alert.payload, dict) else {}
            try:
                last = fields.Datetime.to_datetime(payload.get("notified"))
            except (TypeError, ValueError):
                last = None
            if (last or alert.create_date) > now - NOTICE_EVERY:
                return False
        return True

    def _security_text(self, code, host, detail, count, since, at):
        """Return the alert's text, in the language of this environment.

        :param str code: the alert's code.
        :param host: the ``cloud.host``.
        :param dict detail: the last event's sanitized fields.
        :param int count: events since the first.
        :param datetime since: the first's moment.
        :param datetime at: the last's moment.
        :rtype: str
        """
        _ = self.env._
        values = {
            "host": host.name, "count": count, "since": _clock(since),
            "time": _clock(at),
        }
        if code in (CODE_FOREIGN, CODE_ELSEWHERE):
            values.update(user=detail["user"], ip=detail["ip"])
        if code == CODE_FOREIGN:
            values["reason"] = self._security_reason(
                detail.get("reason"), detail.get("key_label"),
            )
            if count == 1:
                return _(
                    "%(host)s: SSH sign-in as %(user)s from %(ip)s at "
                    "%(time)s %(reason)s. If it was not someone you know, see "
                    "Metrics → Security of the server.", **values,
                )
            return _(
                "%(host)s: %(count)s SSH sign-ins the panel did not make "
                "since %(since)s. The last: as %(user)s from %(ip)s at "
                "%(time)s %(reason)s. If they were not someone you know, see "
                "Metrics → Security of the server.", **values,
            )
        if code == CODE_ELSEWHERE:
            if count == 1:
                return _(
                    "%(host)s: SSH sign-in as %(user)s with the panel's key "
                    "from %(ip)s at %(time)s, an address the panel does not "
                    "connect from (checked with a connection of its own). The "
                    "key may have been copied: see Metrics → Security of the "
                    "server.", **values,
                )
            return _(
                "%(host)s: %(count)s SSH sign-ins with the panel's key from "
                "addresses the panel does not connect from since %(since)s "
                "(checked with a connection of its own). The last: as "
                "%(user)s from %(ip)s at %(time)s. The key may have been "
                "copied: see Metrics → Security of the server.", **values,
            )
        if code == CODE_UNCONFIRMED:
            if count == 1:
                return _(
                    "%(host)s: SSH sign-in at %(time)s from an address the "
                    "panel was not seen using, and the panel could not "
                    "connect to the server to check whether it is its own. "
                    "See Metrics → Security of the server.", **values,
                )
            return _(
                "%(host)s: %(count)s SSH sign-ins since %(since)s from "
                "addresses the panel was not seen using, and the panel could "
                "not connect to the server to check whether they are its "
                "own; the last at %(time)s. See Metrics → Security of the "
                "server.", **values,
            )
        if code == CODE_CHANGE:
            values.update(
                file=detail.get("file") or _("a watched file whose name did not arrive"),
                program=detail.get("program") or _("an unknown program"),
                who=self._security_who_text(detail),
            )
            if count == 1:
                return _(
                    "%(host)s: %(file)s was changed at %(time)s by %(who)s "
                    "with %(program)s. See Metrics → Security of the server.",
                    **values,
                )
            return _(
                "%(host)s: %(count)s changes to watched files the panel did "
                "not make since %(since)s. The last: %(file)s at %(time)s by "
                "%(who)s with %(program)s. See Metrics → Security of the "
                "server.", **values,
            )
        values.update(
            user=detail["user"],
            program=detail.get("program") or _("a command that cannot be read for sure"),
        )
        if count > 1:
            return _(
                "%(host)s: %(count)s sudo commands by users the panel does "
                "not sign in as since %(since)s. The last: %(user)s, "
                "%(program)s, at %(time)s. See Metrics → Security of the "
                "server.", **values,
            )
        if detail.get("kind") == "refused":
            return _(
                "%(host)s: sudo refused %(program)s to %(user)s, who is not "
                "the user the panel signs in as, at %(time)s. See Metrics → "
                "Security of the server.", **values,
            )
        return _(
            "%(host)s: %(user)s, who is not the user the panel signs in as, "
            "ran %(program)s with sudo at %(time)s. See Metrics → Security "
            "of the server.", **values,
        )

    def _security_reason(self, reason, label=None):
        """Return why a sign-in is not the panel's, as the text says it.

        :param str reason: ``key``, ``password``, ``user``, ``address`` or
            ``known_key_address``.
        :param str label: the known key's name, for ``known_key_address``.
        :rtype: str
        """
        _ = self.env._
        if reason == "known_key_address":
            return _(
                "with the known key «%(label)s» from an address it may not "
                "sign in from", label=label or "",
            )
        if reason == "password":
            return _("with a password, while the panel signs in to it with a key")
        if reason == "user":
            return _("as a user the panel does not sign in as")
        if reason == "address":
            return _(
                "as the panel's user from an address that is not the "
                "panel's (checked with a connection of its own)"
            )
        return _("with a key that is not the panel's")

    def _security_who_text(self, detail):
        """Return who made a change, as the text says it.

        :param dict detail: the change's fields.
        :rtype: str
        """
        _ = self.env._
        who = detail.get("who")
        if who == "user":
            return _(
                "a user other than the panel's (login uid %(auid)s)",
                auid=detail.get("auid"),
            )
        if who == "root":
            return _("root, outside every job and console of the panel")
        return _("the panel's user, outside every job and console of the panel")

    def _security_backlog_text(self, query):
        """Return the backlog warning's text for one of the reads.

        :param str query: one of :data:`QUERIES`.
        :rtype: str
        """
        _ = self.env._
        what = {
            "accepted": _("SSH sign-ins"),
            "sudo": _("sudo commands"),
            "auditd": _("changes to watched files"),
        }[query]
        return _(
            "The servers send more %(what)s than the security events' check "
            "reads in a pass: two passes in a row stopped at %(lines)s lines, "
            "so their alerts come late. They are read in order and none is "
            "skipped.", what=what, lines=logs.MAX_PAGES * logs.PAGE_LIMIT,
        )


def _int_param(raw):
    """Return a parameter's value as a non-negative int, or None.

    :param raw: what ``ir.config_parameter`` holds.
    :rtype: int | None
    """
    raw = str(raw or "").strip()
    return int(raw) if raw.isdigit() else None
