"""The alerts of the servers' security events (plan #84, cut C4).

The cron reads sign-ins, sudo and auditd from Loki with three queries,
each with its cursor, and raises what the panel did not do. The reader
of Loki and the connection that confirms the panel's address are
``MagicMock(spec=…)`` of the real classes: the tests hand them lines and
answers, and look at the alerts, the cursors and what was asked.
"""
import contextlib
import re
import time
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.sql_db import Cursor
from odoo.tests.common import TransactionCase
from odoo.tools import translate

from odoo.addons.incubacloud.models import logs
from odoo.addons.incubacloud.models import cloud_security_events as events
from odoo.addons.incubacloud.models.cloud_security_events import PanelSourceProbe

PANEL_FP = "SHA256:" + "P" * 43
OTHER_FP = "SHA256:" + "O" * 43
OLD_FP = "SHA256:" + "Q" * 43
PANEL_IP = "198.51.100.10"
NEW_PANEL_IP = "198.51.100.20"
STRANGER_IP = "203.0.113.66"
SECOND = 1_000_000_000
MINUTE = 60 * SECOND
CODES = (
    events.CODE_FOREIGN, events.CODE_ELSEWHERE, events.CODE_UNCONFIRMED,
    events.CODE_CHANGE, events.CODE_SUDO, events.CODE_BACKLOG,
)


def _accepted(ip, fingerprint=PANEL_FP, user="incubacloud"):
    """Return sshd's line of a sign-in, with a key or, without one, a password.

    :rtype: str
    """
    if fingerprint is None:
        return f"Accepted password for {user} from {ip} port 50000 ssh2"
    return (
        f"Accepted publickey for {user} from {ip} port 50000 ssh2: "
        f"ED25519 {fingerprint}"
    )


def _sudo(user="probador", command="/usr/bin/cat /etc/shadow --token=hunter2"):
    """Return sudo's line of a command.

    :rtype: str
    """
    return (
        f"{user} : TTY=pts/0 ; PWD=/home/{user} ; USER=root ; "
        f"COMMAND={command}"
    )


def _change(ns, auid, serial=900):
    """Return auditd's two records of a change to a sudoers file at *ns*.

    :rtype: list[str]
    """
    stamp = f"{ns // SECOND}.000:{serial}"
    return [
        f"type=SYSCALL msg=audit({stamp}): arch=c000003e syscall=257 "
        f"success=yes exit=3 ppid=10 pid=11 auid={auid} uid=0 gid=0 "
        'comm="tee" exe="/usr/bin/tee" key="sudoers_changes"',
        f"type=PATH msg=audit({stamp}): item=1 "
        'name="/etc/sudoers.d/99-qa" inode=5 nametype=CREATE',
    ]


class SecurityEventsCase(TransactionCase):
    """A server sending its security logs, Loki and the probe in hand."""

    def setUp(self):
        """One server of the panel, the cursors a pass old, no alert open."""
        super().setUp()
        # The cron writes the servers' rows on a READ COMMITTED cursor of
        # its own: in test mode it rides on this transaction, outside it
        # would be a connection that sees nothing of the test.
        self.registry_enter_test_mode()
        self.Events = self.env["cloud.security.events"]
        self.now = time.time_ns()
        self.lines = {"accepted": [], "sudo": [], "auditd": []}
        self.incomplete = set()
        self.down = False
        self.reads = []
        self.fingerprints = {}
        self.reader = MagicMock(spec=logs.LogReader)
        self.reader.read_lines.side_effect = self._read
        self.reader.query.return_value = []
        self.probe = MagicMock(spec=PanelSourceProbe)
        self.probe.read.return_value = PANEL_IP
        cls = type(self.Events)
        for name, value in (
            ("_security_reader", self.reader), ("_security_probe", self.probe),
        ):
            patcher = patch.object(cls, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(cls, "_security_now_ns", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(
            type(self.env["cloud.host"]), "_panel_key_fingerprints",
            autospec=True,
            side_effect=lambda host: self.fingerprints.get(host.id, [PANEL_FP]),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = self._host("S1")
        self.env["cloud.alert"].sudo().search([
            ("code", "in", CODES), ("state", "=", "active"),
        ]).write({"state": "dismissed"})
        self.env["cloud.security.event"].sudo().search([]).unlink()
        for query in events.QUERIES:
            self._cursor(query, self.now - 5 * MINUTE)
            self.env["ir.config_parameter"].sudo().set_param(
                events.BACKLOG_PARAM % query, "0",
            )

    def _host(self, name, **vals):
        """Create a server sending its security logs with C1's agents.

        :rtype: cloud.host
        """
        self._made = getattr(self, "_made", 0) + 1
        host = self.env["cloud.host"].sudo().create({
            "name": name,
            "ip_address": f"10.77.250.{self._made}",
            "user": "incubacloud",
            "wildcard_domain": f"{name.lower()}.example.com",
        } | vals)
        host.write({
            "security_logs_enabled": True,
            "metrics_agents_state": "installed",
            "metrics_agents_revision": 4,
            "metrics_write_user": "agent",
            "security_panel_uid": "1001",
        })
        host._record_panel_source(PANEL_IP)
        return host

    def _cursor(self, query, value=None):
        """Set a read's cursor, or return it.

        :rtype: int | None
        """
        params = self.env["ir.config_parameter"].sudo()
        if value is not None:
            params.set_param(events.CURSOR_PARAM % query, str(value))
            return value
        raw = params.get_param(events.CURSOR_PARAM % query)
        return int(raw) if raw else None

    def _add(self, query, line, ago=2 * MINUTE, host=None):
        """Give Loki a line of *query* from *host*, *ago* before now.

        :rtype: int
        """
        ns = self.now - ago
        source = {"accepted": "sshd"}.get(query, query)
        self.lines[query].append(
            logs.LogLine((host or self.host).id, ns, line, source),
        )
        return ns

    def _read(self, expression, start, end, max_pages=logs.MAX_PAGES,
              limit=logs.PAGE_LIMIT):
        """Answer a read as Loki would, from the lines given.

        :rtype: logs.LogRead
        """
        self.reads.append((expression, start, end))
        if self.down:
            raise requests.ConnectionError("central down")
        query = (
            "accepted" if 'source="sshd"' in expression
            else "sudo" if 'source="sudo"' in expression else "auditd"
        )
        ids = {int(i) for i in re.search(r'host_id=~?"([0-9|]+)"', expression)[1].split("|")}
        lines = sorted(
            (line for line in self.lines[query]
             if line.host_id in ids and start <= line.ns < end),
            key=lambda line: line.ns,
        )
        if query in self.incomplete and lines:
            return logs.LogRead(lines, lines[-1].ns, False)
        return logs.LogRead(lines, lines[-1].ns if lines else None, True)

    def _run(self, **context):
        """Run one pass of the cron."""
        # The cron's own cursor reads the database, not this env's
        # pending writes.
        self.env.flush_all()
        self.Events.with_context(**context)._cron_security_events()

    def _alerts(self, code, host=None):
        """Return the active alerts of *code* on *host*.

        :rtype: cloud.alert
        """
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", code), ("state", "=", "active"),
            ("host_id", "=", (host or self.host).id),
        ])


class TestSignIns(SecurityEventsCase):
    """Who signed in: the panel's key and address, or someone else."""

    def test_a_foreign_key_is_critical_and_another_counts(self):
        """A key that is not the panel's; a second one adds to the count."""
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP, user="root"))
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.assertEqual(alert.level, "critical")
        self.assertIn(STRANGER_IP, alert.message)
        self.assertIn("as root", alert.message)
        self.assertIn("with a key that is not the panel's", alert.message)
        self.assertIn("Metrics → Security", alert.message)
        self.assertEqual(alert.payload["count"], 1)
        self._add("accepted", _accepted("203.0.113.67", OTHER_FP), ago=MINUTE)
        self._run()
        self.assertEqual(len(self._alerts(events.CODE_FOREIGN)), 1)
        self.assertEqual(alert.payload["count"], 2)
        self.assertEqual(alert.occurrences, 2)
        self.assertIn("2 SSH sign-ins the panel did not make", alert.message)
        self.assertIn("203.0.113.67", alert.message)
        self.probe.read.assert_not_called()

    def test_a_password_where_the_panel_uses_a_key_is_foreign(self):
        """With a key on the server, a password sign-in is not the panel's."""
        self._add("accepted", _accepted(PANEL_IP, fingerprint=None))
        self._run()
        self.assertIn("with a password", self._alerts(events.CODE_FOREIGN).message)

    def test_the_key_replaced_counts_until_the_change(self):
        """The key a change replaced is the panel's for a sign-in from before it (B1)."""
        self.host._remember_previous_key([OLD_FP])
        host = self.host.sudo()
        self.assertEqual(host.security_previous_key_fingerprint, OLD_FP)
        self.assertTrue(host.security_key_changed_at)
        # The same key before and after is no change.
        self.host._remember_previous_key([PANEL_FP])
        self.assertEqual(host.security_previous_key_fingerprint, OLD_FP)
        changed_ns = self.now - 10 * MINUTE
        host.security_key_changed_at = events._to_datetime(changed_ns)
        changed_ns = events._to_ns(host.security_key_changed_at)
        self._add("accepted", _accepted(PANEL_IP, OLD_FP),
                  ago=self.now - changed_ns + 30 * SECOND)
        self._run()
        self.assertFalse(self._alerts(events.CODE_FOREIGN))
        self._add("accepted", _accepted(PANEL_IP, OLD_FP),
                  ago=self.now - changed_ns - 2 * MINUTE)
        self._run()
        self.assertIn("with a key that is not the panel's",
                      self._alerts(events.CODE_FOREIGN).message)

    def test_a_change_of_key_is_remembered(self):
        """Writing the key hands the fingerprints from before to the memory."""
        self.fingerprints[self.host.id] = [OLD_FP]
        with patch.object(
            type(self.env["cloud.host"]), "_remember_previous_key", autospec=True,
        ) as remember:
            self.host.sudo().write({"login_type": self.host.login_type})
        remember.assert_called_once_with(self.host, [OLD_FP])

    def test_the_panel_from_its_own_address_never_alerts(self):
        """The panel's key from its address, outside any job: nothing."""
        self._add("accepted", _accepted(PANEL_IP))
        self._run()
        for code in CODES:
            self.assertFalse(self._alerts(code), code)
        self.probe.read.assert_not_called()

    def test_another_address_confirmed_as_the_panel_s_is_learned(self):
        """The connection reads the same address: learned, no alert (A1)."""
        self.probe.read.return_value = NEW_PANEL_IP
        self._add("accepted", _accepted(NEW_PANEL_IP))
        self._run()
        self.assertFalse(self._alerts(events.CODE_ELSEWHERE))
        self.assertFalse(self._alerts(events.CODE_UNCONFIRMED))
        self.assertTrue(self.host._panel_source_known(NEW_PANEL_IP))
        self.probe.read.assert_called_once()

    def test_another_address_the_connection_denies_is_critical(self):
        """The panel's key from an address the connection says is not its own."""
        self._add("accepted", _accepted(STRANGER_IP))
        self._run()
        alert = self._alerts(events.CODE_ELSEWHERE)
        self.assertEqual(alert.level, "critical")
        self.assertIn(STRANGER_IP, alert.message)
        self.assertFalse(self.host._panel_source_known(STRANGER_IP))
        # What the connection read is the panel's, and is learned.
        self.assertTrue(self.host._panel_source_known(PANEL_IP))

    def test_no_connection_is_a_warning_unconfirmed(self):
        """Without the connection, a warning without the address (D2)."""
        self.probe.read.side_effect = OSError("no route to host")
        self._add("accepted", _accepted(STRANGER_IP))
        with self.assertLogs(events.__name__, "WARNING"):
            self._run()
        alert = self._alerts(events.CODE_UNCONFIRMED)
        self.assertEqual(alert.level, "warning")
        self.assertNotIn(STRANGER_IP, alert.message)
        self.assertNotIn(STRANGER_IP, str(alert.payload))
        self.assertFalse(self._alerts(events.CODE_ELSEWHERE))

    def test_a_change_of_the_panel_s_address_raises_nothing(self):
        """Three servers see the panel from a new address: no critical (A1)."""
        second, third = self._host("S2"), self._host("S3")
        self.probe.read.return_value = NEW_PANEL_IP
        for host in (self.host, second, third):
            self._add("accepted", _accepted(NEW_PANEL_IP), host=host)
        self._run()
        for host in (self.host, second, third):
            for code in CODES:
                self.assertFalse(self._alerts(code, host), (host.name, code))
            self.assertTrue(host._panel_source_known(NEW_PANEL_IP))
        # One connection confirmed the address for all three.
        self.assertEqual(self.probe.read.call_count, 1)

    def test_with_no_connection_left_the_sign_in_waits(self):
        """Past the pass's connections, the cursor stops before the sign-in."""
        hosts = [self._host(f"W{n}") for n in range(events.PROBES_PER_PASS + 1)]
        self.probe.read.side_effect = lambda host: f"198.51.100.{100 + host.id % 100}"
        for n, host in enumerate(hosts):
            self._add("accepted", _accepted(f"203.0.113.{n + 1}"), host=host,
                      ago=3 * MINUTE - n * SECOND)
        self._run()
        self.assertEqual(self.probe.read.call_count, events.PROBES_PER_PASS)
        waiting = self.lines["accepted"][-1].ns
        self.assertEqual(self._cursor("accepted"), waiting)
        self.assertFalse(self._alerts(events.CODE_ELSEWHERE, hosts[-1]))

    def test_a_password_server_by_user_and_address(self):
        """Without a key, the panel is its user from its address (A4)."""
        self.fingerprints[self.host.id] = []
        self._add("accepted", _accepted(PANEL_IP, fingerprint=None))
        self._run()
        self.assertFalse(self._alerts(events.CODE_FOREIGN))
        self._add("accepted", _accepted(PANEL_IP, fingerprint=None, user="root"),
                  ago=MINUTE)
        self._run()
        self.assertIn("as a user the panel does not sign in as",
                      self._alerts(events.CODE_FOREIGN).message)
        self._alerts(events.CODE_FOREIGN).write({"state": "dismissed"})
        self._add("accepted", _accepted(STRANGER_IP, fingerprint=None),
                  ago=MINUTE - SECOND)
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.assertIn("from an address that is not the panel's", alert.message)
        self.assertIn(STRANGER_IP, alert.message)


class TestSudoAndChanges(SecurityEventsCase):
    """sudo by another user, and changes to the watched files."""

    def test_sudo_by_another_user_without_its_arguments(self):
        """The program and not its arguments; the payload holds no line."""
        line = _sudo()
        self._add("sudo", line)
        self._run()
        alert = self._alerts(events.CODE_SUDO)
        self.assertEqual(alert.level, "warning")
        self.assertIn("/usr/bin/cat", alert.message)
        self.assertNotIn("hunter2", alert.message)
        self.assertNotIn("/etc/shadow", alert.message)
        self.assertNotIn("hunter2", str(alert.payload))
        self.assertNotIn(line, str(alert.payload))
        self.assertNotIn(self.host.ip_address, str(alert.payload))

    def test_the_panel_s_sudo_is_left_out_in_loki(self):
        """The panel's user's commands are excluded in the query itself."""
        self._add("sudo", _sudo(user="incubacloud"))
        self._run()
        self.assertFalse(self._alerts(events.CODE_SUDO))
        (expression,) = [e for e, *_ in self.reads if 'source="sudo"' in e]
        self.assertIn(r'!~ "^\\s*incubacloud : "', expression)

    def test_a_change_in_a_job_is_quiet_and_outside_critical(self):
        """sudoers changed by the panel inside a job, then with none open."""
        job_type = self.env["cloud.job.type"].search([], limit=1)
        job = self.env["cloud.job"].sudo().create({
            "name": "Hardening", "host_id": self.host.id,
            "job_type_id": job_type.id,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'started', "
            "create_date = now() at time zone 'UTC' - interval '10 minutes' "
            "WHERE id = %s", (job.id,),
        )
        job.invalidate_recordset()
        for line in _change(self.now - 2 * MINUTE, auid=1001):
            self._add("auditd", line)
        self._run()
        self.assertFalse(self._alerts(events.CODE_CHANGE))
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'done', "
            "create_date = now() at time zone 'UTC' - interval '3 hours', "
            "write_date = now() at time zone 'UTC' - interval '2 hours' "
            "WHERE id = %s", (job.id,),
        )
        job.invalidate_recordset()
        # Later, the first change out of the window: only the second is read.
        self.now += 20 * MINUTE
        self._cursor("auditd", self.now - 90 * SECOND)
        for line in _change(self.now - MINUTE, auid=1001, serial=901):
            self._add("auditd", line, ago=MINUTE)
        self._run()
        alert = self._alerts(events.CODE_CHANGE)
        self.assertEqual(alert.level, "critical")
        self.assertIn("/etc/sudoers.d/99-qa", alert.message)
        self.assertIn("the panel's user", alert.message)
        self.assertEqual(alert.payload["count"], 1)

    def test_a_console_is_a_window_too(self):
        """A console of the panel open on the server: its changes are quiet."""
        self.env["cloud.host.session"].sudo().create({
            "host_id": self.host.id, "user_id": self.env.user.id,
            "session_id": "security-events-console",
            "opened_at": events._to_datetime(self.now - 10 * MINUTE),
        })
        for line in _change(self.now - 2 * MINUTE, auid=1001):
            self._add("auditd", line)
        self._run()
        self.assertFalse(self._alerts(events.CODE_CHANGE))

    def test_another_user_is_critical_even_in_a_window(self):
        """A login uid that is not the panel's is never the panel's."""
        self.env["cloud.host.session"].sudo().create({
            "host_id": self.host.id, "user_id": self.env.user.id,
            "session_id": "security-events-console-2",
            "opened_at": events._to_datetime(self.now - 10 * MINUTE),
        })
        for line in _change(self.now - 2 * MINUTE, auid=1005):
            self._add("auditd", line)
        self._run()
        alert = self._alerts(events.CODE_CHANGE)
        self.assertEqual(alert.level, "critical")
        self.assertIn("login uid 1005", alert.message)

    def test_root_outside_a_window_takes_its_provisional_level(self):
        """auid 0 outside every window: the level step M-b fixes (M7)."""
        for line in _change(self.now - 2 * MINUTE, auid=0):
            self._add("auditd", line)
        self._run()
        self.assertEqual(
            self._alerts(events.CODE_CHANGE).level, events.ROOT_CHANGE_LEVEL,
        )

    def test_the_system_s_own_changes_do_not_alert(self):
        """No login uid: the system's own, shown in the viewer only."""
        for line in _change(self.now - 2 * MINUTE, auid=logs.AUID_UNSET):
            self._add("auditd", line)
        self._run()
        self.assertFalse(self._alerts(events.CODE_CHANGE))


class TestCursors(SecurityEventsCase):
    """Where each read starts, stops and goes on."""

    def test_without_a_cursor_nothing_earlier_is_alerted(self):
        """The first pass sets the cursors to now and reads nothing (M1)."""
        params = self.env["ir.config_parameter"].sudo()
        for query in events.QUERIES:
            params.set_param(events.CURSOR_PARAM % query, False)
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        self.assertFalse(self._alerts(events.CODE_FOREIGN))
        self.assertEqual(self._cursor("accepted"), self.now - events.SETTLE_NS)
        self.reader.read_lines.assert_not_called()

    def test_the_window_overlaps_and_ends_before_now(self):
        """From 15 minutes before the cursor to 30 seconds ago."""
        cursor = self._cursor("accepted")
        self._run()
        (start, end), = [(s, e) for x, s, e in self.reads if 'source="sshd"' in x]
        self.assertEqual(start, cursor - events.OVERLAP_NS)
        self.assertEqual(end, self.now - events.SETTLE_NS)
        self.assertEqual(self._cursor("accepted"), end)

    def test_a_central_down_leaves_the_cursor(self):
        """A failed read moves nothing; the next reads the same window."""
        cursor = self._cursor("accepted")
        self.down = True
        with self.assertLogs(events.__name__, "WARNING"):
            self._run()
        self.assertEqual(self._cursor("accepted"), cursor)

    def test_after_a_long_outage_the_window_reaches_24_hours(self):
        """A cursor two days old reads the last 24 hours."""
        self._cursor("accepted", self.now - 48 * 3600 * SECOND)
        self._run()
        (start,) = [s for x, s, _e in self.reads if 'source="sshd"' in x]
        self.assertEqual(start, self.now - events.SETTLE_NS - events.LOOKBACK_NS)

    def test_at_the_page_limit_the_cursor_stays_at_the_last_line(self):
        """Stopped by the limit, the cursor is the last line; twice, a warning (A3)."""
        self.incomplete.add("sudo")
        last = self._add("sudo", _sudo(), ago=4 * MINUTE)
        self._run()
        self.assertEqual(self._cursor("sudo"), last)
        self.assertFalse(self.env["cloud.alert"].sudo().search([
            ("code", "=", events.CODE_BACKLOG), ("state", "=", "active"),
        ]))
        self._run()
        # Behind, the window starts at the cursor itself.
        starts = [s for x, s, _e in self.reads if 'source="sudo"' in x]
        self.assertEqual(starts[-1], last)
        backlog = self.env["cloud.alert"].sudo().search([
            ("code", "=", events.CODE_BACKLOG), ("state", "=", "active"),
            ("dedup_key", "=", "sudo"),
        ])
        self.assertEqual(backlog.level, "warning")
        self.incomplete.clear()
        self._run()
        self.assertEqual(backlog.state, "dismissed")

    def test_with_no_server_left_the_backlog_closes(self):
        """Behind, then every server switched off: nothing is behind (B3)."""
        self.incomplete.add("sudo")
        self._add("sudo", _sudo(), ago=4 * MINUTE)
        self._run()
        self._run()
        backlog = self.env["cloud.alert"].sudo().search([
            ("code", "=", events.CODE_BACKLOG), ("state", "=", "active"),
            ("dedup_key", "=", "sudo"),
        ])
        self.assertTrue(backlog)
        self.host.write({"security_logs_enabled": False})
        self._run()
        self.assertEqual(backlog.state, "dismissed")

    def test_sudo_noise_does_not_hide_a_sign_in(self):
        """sudo stopped at its limit; the sign-in of the same pass is seen (A3)."""
        self.incomplete.add("sudo")
        for n in range(20):
            self._add("sudo", _sudo(command=f"/bin/true {n}"), ago=3 * MINUTE - n)
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        self.assertTrue(self._alerts(events.CODE_FOREIGN))

    def test_a_line_read_twice_counts_once(self):
        """The overlap brings the same sign-in again: still one."""
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP), ago=2 * MINUTE)
        self._run()
        self.now += 5 * MINUTE
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.assertEqual(alert.payload["count"], 1)
        # Dismissed, the same event does not raise it again.
        alert.write({"state": "dismissed"})
        self.now += 5 * MINUTE
        self._run()
        self.assertFalse(self._alerts(events.CODE_FOREIGN))

    def test_a_server_switched_off_is_not_read(self):
        """Its id is in no query; nor a server without C1's agents."""
        off = self._host("OFF")
        off.write({"security_logs_enabled": False})
        old = self._host("OLD")
        old.write({"metrics_write_user": False})
        self._run()
        for expression, _start, _end in self.reads:
            ids = re.search(r'host_id=~?"([0-9|]+)"', expression)[1].split("|")
            self.assertIn(str(self.host.id), ids)
            self.assertNotIn(str(off.id), ids)
            self.assertNotIn(str(old.id), ids)

    def test_a_line_of_a_server_not_read_is_ignored(self):
        """A line Loki gives for a server not asked for raises nothing."""
        self.lines["accepted"].append(logs.LogLine(
            self.host.id + 100000, self.now - MINUTE,
            _accepted(STRANGER_IP, OTHER_FP), "sshd",
        ))
        self.reader.read_lines.side_effect = lambda expression, start, end, **kw: (
            logs.LogRead(
                [line for line in self.lines["accepted"] if 'source="sshd"' in expression],
                self.now - MINUTE, True,
            )
        )
        self._run()
        self.assertFalse(self.env["cloud.alert"].sudo().search([
            ("code", "=", events.CODE_FOREIGN), ("state", "=", "active"),
        ]))

    def test_a_server_back_from_a_silence_is_read_again(self):
        """Silent two hours, it speaks: read alone from where it went quiet (M1)."""
        self.host.write({
            "security_last_line_at": events._to_datetime(self.now - 2 * 3600 * SECOND),
        })
        quiet = events._to_ns(self.host.security_last_line_at)
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP), ago=90 * MINUTE)
        self.reader.query.return_value = [({"host_id": str(self.host.id)}, 12.0)]
        self._run()
        self.assertTrue(self._alerts(events.CODE_FOREIGN))
        replay = [
            (s, e) for x, s, e in self.reads
            if 'source="sshd"' in x and f'host_id="{self.host.id}"' in x
            and s < self.now - 60 * MINUTE
        ]
        self.assertEqual(replay, [(quiet - events.OVERLAP_NS, self.now - events.SETTLE_NS)])
        self.assertEqual(
            self.host.security_last_line_at,
            events._to_datetime(self.now - events.SETTLE_NS).replace(microsecond=0),
        )
        (expression,), kwargs = self.reader.query.call_args
        self.assertIn('source=~"sshd|sudo|fail2ban|auditd"', expression)
        self.assertEqual(kwargs["at_ns"], self.now - events.SETTLE_NS)

    def test_a_server_seen_for_the_first_time_is_only_noted(self):
        """No line seen before: nothing is read again."""
        self.reader.query.return_value = [({"host_id": str(self.host.id)}, 3.0)]
        self._run()
        self.assertTrue(self.host.security_last_line_at)
        self.assertEqual(
            len([x for x, *_ in self.reads if 'source="sshd"' in x]), 1,
        )


class TestTexts(SecurityEventsCase):
    """In the language of the cron's context; a broken one falls back."""

    def test_in_spanish(self):
        """With Spanish in the context, the alert is written in Spanish."""
        self.env["res.lang"]._activate_lang("es_ES")
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP, user="root"))
        self._run(lang="es_ES")
        message = self._alerts(events.CODE_FOREIGN).message
        self.assertIn("inicio de sesión SSH como root", message)
        self.assertIn("con una clave que no es la del panel", message)

    def test_a_translation_with_broken_placeholders_gives_english(self):
        """A translation that breaks its placeholders: the English text (M6)."""
        self.env["res.lang"]._activate_lang("es_ES")
        source = (
            "%(host)s: SSH sign-in as %(user)s from %(ip)s at %(time)s "
            "%(reason)s. If it was not someone you know, see Metrics → "
            "Security of the server."
        )
        with patch.object(
            translate.code_translations, "get_python_translations",
            return_value={source: "%(anfitrion)s: roto"},
        ), self.assertLogs("odoo.tools.translate", "ERROR"):
            self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
            self._run(lang="es_ES")
        message = self._alerts(events.CODE_FOREIGN).message
        self.assertTrue(message.startswith("S1: SSH sign-in as incubacloud"))


#: The table a writing statement writes, whatever the statement.
_WRITE = re.compile(
    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(\w+)\"?",
    re.IGNORECASE,
)


class TestHostRowsApart(SecurityEventsCase):
    """The cron's transaction never writes a server's row (final review, M1).

    ``security_last_line_at`` and the panel's addresses are written on a
    READ COMMITTED cursor of their own: under the cron's snapshot, a
    job's hook or a save of the server writing the same row made one of
    the two lose it, and a pass that lost rolled back with its alerts
    already sent.
    """

    def _writes(self):
        """Run the cron and sort what it wrote by the cursor writing it.

        :return: ``(main, separate)``, the tables written by the cron's
            transaction and by its READ COMMITTED cursors.
        """
        self.env.flush_all()
        main, separate = set(), set()
        inside = []
        real_open = events.read_committed_cursor
        real_execute = Cursor.execute

        @contextlib.contextmanager
        def tracked(registry):
            """Open the real cursor and note while it is in use."""
            inside.append(True)
            try:
                with real_open(registry) as cr:
                    yield cr
            finally:
                inside.pop()

        def spy(cr, query, params=None, *args, **kwargs):
            """Note the table of every write, by the cursor it is on."""
            code = getattr(query, "code", query)
            found = _WRITE.match(code if isinstance(code, str) else "")
            if found:
                (separate if inside else main).add(found.group(1))
            return real_execute(cr, query, params, *args, **kwargs)

        with patch.object(
            events, "read_committed_cursor", side_effect=tracked,
        ), patch.object(Cursor, "execute", autospec=True, side_effect=spy):
            self._run()
            # What the cron's transaction left in its cache would be
            # written by its commit.
            self.env.flush_all()
        return main, separate

    def test_last_line_and_learned_address_are_written_apart(self):
        """A server back from a silence, one seen first, an address learned."""
        self.host.write({
            "security_last_line_at": events._to_datetime(self.now - 2 * 3600 * SECOND),
        })
        other = self._host("S2")
        self.reader.query.return_value = [
            ({"host_id": str(self.host.id)}, 12.0),
            ({"host_id": str(other.id)}, 3.0),
        ]
        self.probe.read.return_value = NEW_PANEL_IP
        self._add("accepted", _accepted(NEW_PANEL_IP))
        main, separate = self._writes()
        self.assertIn("cloud_host", separate)
        self.assertNotIn("cloud_host", main)
        self.assertEqual(
            self.host.security_last_line_at,
            events._to_datetime(self.now - events.SETTLE_NS).replace(microsecond=0),
        )
        self.assertTrue(other.security_last_line_at)
        self.assertTrue(self.host._panel_source_known(NEW_PANEL_IP))
        self.assertTrue(self.host._panel_source_known(PANEL_IP))


class TestOneEventAtATime(SecurityEventsCase):
    """A line that breaks its judgement stops nothing else (final review, M2)."""

    def test_an_auditd_stamp_out_of_range_takes_loki_s_instant(self):
        """Twelve digits of seconds, written by hand: no datetime holds them."""
        ns = self.now - 2 * MINUTE
        for line in _change(ns, auid=1005):
            self._add("auditd", line.replace(f"audit({ns // SECOND}.", "audit(999999999999."))
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        change = self._alerts(events.CODE_CHANGE)
        self.assertEqual(change.level, "critical")
        self.assertEqual(
            change.payload["last"]["time"],
            fields.Datetime.to_string(events._to_datetime(ns)),
        )
        self.assertTrue(self._alerts(events.CODE_FOREIGN))
        for query in events.QUERIES:
            self.assertEqual(self._cursor(query), self.now - events.SETTLE_NS, query)
        # The next pass reads the same lines again: nothing counts twice.
        self.now += MINUTE
        self._run()
        self.assertEqual(self._alerts(events.CODE_CHANGE).payload["count"], 1)
        self.assertEqual(self._alerts(events.CODE_FOREIGN).payload["count"], 1)

    def test_an_event_that_fails_is_logged_and_skipped(self):
        """sudo's judgement raises: the sign-in and the change still alert."""
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._add("sudo", _sudo())
        for line in _change(self.now - 2 * MINUTE, auid=1005):
            self._add("auditd", line)
        with patch.object(
            type(self.Events), "_security_sudo", autospec=True,
            side_effect=ValueError("a line no judgement expected"),
        ), self.assertLogs(events.__name__, "ERROR") as logged:
            self._run()
        self.assertIn("skipped", "\n".join(logged.output))
        self.assertFalse(self._alerts(events.CODE_SUDO))
        self.assertTrue(self._alerts(events.CODE_FOREIGN))
        self.assertTrue(self._alerts(events.CODE_CHANGE))
        for query in events.QUERIES:
            self.assertEqual(self._cursor(query), self.now - events.SETTLE_NS, query)
        # The pass was kept: the next one counts nothing again.
        self.now += MINUTE
        self._run()
        self.assertEqual(self._alerts(events.CODE_FOREIGN).payload["count"], 1)


class TestRootPanel(SecurityEventsCase):
    """Where the panel signs in as root, login uid 0 is root (final review, B5)."""

    def test_root_s_change_outside_a_window_takes_root_s_level(self):
        """Not the panel's critical: root's scheduled tasks carry uid 0 too."""
        self.host.write({"security_panel_uid": "0"})
        for line in _change(self.now - 2 * MINUTE, auid=0):
            self._add("auditd", line)
        self._run()
        alert = self._alerts(events.CODE_CHANGE)
        self.assertEqual(alert.level, events.ROOT_CHANGE_LEVEL)
        self.assertEqual(alert.payload["last"]["who"], "root")


class TestNotices(SecurityEventsCase):
    """An open event alert notifies again, once an hour per server (D8)."""

    def setUp(self):
        """Count what reaches the channels, alert by alert."""
        super().setUp()
        patcher = patch.object(
            type(self.env["cloud.alert"]), "_dispatch_notifications",
            autospec=True,
        )
        self.dispatch = patcher.start()
        self.addCleanup(patcher.stop)

    def _notified(self, alert):
        """Return how many times *alert* went to the channels.

        :rtype: int
        """
        return sum(
            1 for call in self.dispatch.call_args_list if call.args[0] == alert
        )

    def test_a_second_sign_in_within_the_hour_only_refreshes(self):
        """The alert counts it and says it; the channels hear nothing."""
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.assertEqual(self._notified(alert), 1)
        self.now += 30 * MINUTE
        self._add("accepted", _accepted("203.0.113.67", OTHER_FP))
        self._run()
        self.assertEqual(alert.payload["count"], 2)
        self.assertIn("203.0.113.67", alert.message)
        self.assertEqual(self._notified(alert), 1)

    def test_past_the_hour_a_new_sign_in_notifies_again(self):
        """Once an hour, and the next within it is quiet again."""
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.now += 61 * MINUTE
        self._add("accepted", _accepted("203.0.113.67", OTHER_FP))
        self._run()
        self.assertEqual(alert.payload["count"], 2)
        self.assertEqual(self._notified(alert), 2)
        self.now += 5 * MINUTE
        self._add("accepted", _accepted("203.0.113.68", OTHER_FP))
        self._run()
        self.assertEqual(alert.payload["count"], 3)
        self.assertEqual(self._notified(alert), 2)

    def test_each_server_has_its_own_hour(self):
        """Two servers notify once each, however many events each has."""
        other = self._host("S2")
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP), host=other)
        self._run()
        first = self._alerts(events.CODE_FOREIGN)
        second = self._alerts(events.CODE_FOREIGN, host=other)
        self.now += 61 * MINUTE
        self._add("accepted", _accepted("203.0.113.67", OTHER_FP), ago=3 * MINUTE)
        self._add("accepted", _accepted("203.0.113.68", OTHER_FP))
        self._add("accepted", _accepted("203.0.113.67", OTHER_FP), host=other)
        self._run()
        self.assertEqual(first.payload["count"], 3)
        self.assertEqual(self._notified(first), 2)
        self.assertEqual(second.payload["count"], 2)
        self.assertEqual(self._notified(second), 2)

    def test_the_hour_is_the_server_s_not_the_alert_s(self):
        """A sudo alert just sent keeps a sign-in's repeat quiet."""
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        login = self._alerts(events.CODE_FOREIGN)
        self.now += 61 * MINUTE
        self._add("sudo", _sudo())
        self._run()
        self.assertEqual(self._notified(self._alerts(events.CODE_SUDO)), 1)
        self.now += 5 * MINUTE
        self._add("accepted", _accepted("203.0.113.67", OTHER_FP))
        self._run()
        self.assertEqual(login.payload["count"], 2)
        self.assertEqual(self._notified(login), 1)

    def test_a_line_read_again_does_not_notify(self):
        """The same sign-in read again an hour later is not a new one."""
        ns = self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.now += 61 * MINUTE
        self._cursor("accepted", ns - SECOND)
        self._run()
        self.assertEqual(alert.payload["count"], 1)
        self.assertEqual(self._notified(alert), 1)
