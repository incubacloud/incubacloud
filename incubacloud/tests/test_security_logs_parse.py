"""Reading the security logs back from Loki, and what each line says.

``models/logs.py`` (plan #84, cut C2): the LogQL reads, paged by time,
and the parsers of sshd, sudo, fail2ban and auditd. The lines are the
real ones of F0 (``tests/fixtures/security_lines/``), and beside them the
hostile ones: what an attacker writes as a user name or a command, which
must neither pass for a login nor reach a message as written.
"""
import pathlib
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models import logs
from odoo.addons.incubacloud.models.cloud_metric_rule import (
    BACKEND_UNREACHABLE_CODE,
    central_busy,
)

_FIXTURES = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "security_lines"
)
_TRANSPORT = "odoo.addons.incubacloud.models.cloud_metric_rule"

BASE = "https://central.example.test/lr/loki"
PANEL_FP = "SHA256:aya+WZfI0ITp74XMk0+4tbzGPANSnRr7f4Xa1l6JSCQ"
OTHER_FP = "SHA256:UCjCc0gMRE5lSQ7xQXTn+efUUYHvSob6Kx+Y8kL9fp0"
SECOND = 1_000_000_000


def _fixture(name):
    """Return the lines of a fixture, its comments left out.

    Split on newlines alone: ``str.splitlines`` also splits on the 0x1D
    of auditd's enriched format.

    :param str name: the fixture's file name.
    :rtype: list[str]
    """
    text = (_FIXTURES / name).read_text(encoding="utf-8")
    return [line for line in text.split("\n") if line and line[0] != "#"]


def _streams(*entries):
    """Build Loki's answer to a log query.

    :param entries: ``(host_id, ns, line)`` or ``(host_id, ns, line,
        source)``, one stream per server and source.
    :rtype: dict
    """
    streams = {}
    for entry in entries:
        host_id, ns, line = entry[:3]
        source = entry[3] if len(entry) > 3 else "auditd"
        stream = streams.setdefault((host_id, source), {
            "stream": {
                "log_type": "security", "host_id": str(host_id),
                "host": f"srv-{host_id}", "source": source,
            },
            "values": [],
        })
        stream["values"].append([str(ns), line])
    return {
        "status": "success",
        "data": {"resultType": "streams", "result": list(streams.values())},
    }


def _responses(*answers):
    """Return ``requests.get`` answers: payloads, statuses or exceptions.

    :rtype: list
    """
    out = []
    for answer in answers:
        if isinstance(answer, BaseException):
            out.append(answer)
            continue
        resp = MagicMock(spec=requests.Response)
        if isinstance(answer, int):
            resp.status_code = answer
            resp.raise_for_status.side_effect = requests.HTTPError(
                f"{answer} error", response=resp,
            )
        else:
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            resp.json.return_value = answer
        out.append(resp)
    return out


class TestRealLines(BaseCase):
    """One line of each shape F0 saw gives the record expected."""

    def test_sshd(self):
        """Logins with the panel's key and another, and both failed endings."""
        accepted_panel, accepted_other, invalid, closed_invalid, closed_auth = (
            _fixture("sshd.txt")
        )
        self.assertEqual(logs.parse_sshd(accepted_panel), {
            "kind": "accepted", "user": "root", "ip": "203.0.113.10",
            "port": 60560, "method": "publickey", "key_type": "ED25519",
            "fingerprint": PANEL_FP,
        })
        self.assertEqual(logs.parse_sshd(accepted_other), {
            "kind": "accepted", "user": "incubacloud", "ip": "127.0.0.1",
            "port": 55722, "method": "publickey", "key_type": "ED25519",
            "fingerprint": OTHER_FP,
        })
        # The line before the ending is not an attempt of its own.
        self.assertIsNone(logs.parse_sshd(invalid))
        self.assertEqual(logs.parse_sshd(closed_invalid), {
            "kind": "failed", "user": "invitado", "ip": "172.31.0.2",
            "port": 53002, "invalid": True,
        })
        self.assertEqual(logs.parse_sshd(closed_auth), {
            "kind": "failed", "user": "root", "ip": "172.31.0.2",
            "port": 53008, "invalid": False,
        })

    def test_sudo(self):
        """A command, Ansible's become and a refused password; PAM's line is none."""
        command, become, pam, refused = _fixture("sudo.txt")
        self.assertEqual(logs.parse_sudo(command), {
            "kind": "command", "user": "incubacloud", "run_as": "root",
            "program": "/usr/bin/true", "become": False, "ambiguous": False,
        })
        self.assertEqual(logs.parse_sudo(become), {
            "kind": "command", "user": "incubacloud", "run_as": "root",
            "program": "/bin/sh", "become": True, "ambiguous": False,
        })
        self.assertIsNone(logs.parse_sudo(pam))
        self.assertEqual(logs.parse_sudo(refused), {
            "kind": "refused", "user": "probador", "run_as": "root",
            "program": "/usr/bin/true", "become": False, "ambiguous": False,
        })

    def test_sudo_rs(self):
        """sudo-rs's command line, two spaces after the colon; PAM's line is none."""
        command, pam = _fixture("sudo_rs.txt")
        self.assertEqual(logs.parse_sudo(command), {
            "kind": "command", "user": "incubacloud", "run_as": "root",
            "program": "/usr/bin/true", "become": False, "ambiguous": False,
        })
        self.assertIsNone(logs.parse_sudo(pam))

    def test_fail2ban(self):
        """A failure found, its ban and its lift."""
        found, ban, unban = _fixture("fail2ban.txt")
        self.assertEqual(logs.parse_fail2ban(found), {
            "jail": "sshd", "action": "found", "ip": "172.31.2.1",
        })
        self.assertEqual(logs.parse_fail2ban(ban), {
            "jail": "sshd", "action": "ban", "ip": "172.31.2.1",
        })
        self.assertEqual(logs.parse_fail2ban(unban), {
            "jail": "sshd", "action": "unban", "ip": "172.31.2.1",
        })

    def test_fail2ban_already_banned_and_daemon_lines(self):
        """``already banned`` is an action; the daemon's own lines are none."""
        self.assertEqual(logs.parse_fail2ban(
            "2026-11-09 18:02:11,004 fail2ban.actions        [6563]: "
            "NOTICE  [sshd] 172.31.2.1 already banned"
        ), {"jail": "sshd", "action": "already_banned", "ip": "172.31.2.1"})
        self.assertIsNone(logs.parse_fail2ban(
            "2026-11-09 17:50:00,001 fail2ban.server         [6563]: "
            "INFO    Starting Fail2ban v1.1.0"
        ))

    def test_auditd(self):
        """The three SYSCALL records by root, the panel's user and the system, and a PATH."""
        useradd, panel_touch, system_touch, path = _fixture("auditd.txt")
        self.assertEqual(logs.parse_auditd(useradd), {
            "type": "SYSCALL", "event": "audit(1794247181.251:525)",
            "time": 1794247181.251, "key": "passwd_changes", "auid": 0,
            "uid": 0, "pid": 8598, "ppid": 8590, "comm": "useradd",
            "exe": "/usr/sbin/useradd", "success": True,
        })
        record = logs.parse_auditd(panel_touch)
        self.assertEqual(
            (record["auid"], record["comm"], record["key"]),
            (1000, "touch", "ssh_config_changes"),
        )
        record = logs.parse_auditd(system_touch)
        self.assertEqual(record["auid"], logs.AUID_UNSET)
        self.assertEqual(record["ppid"], 1)
        self.assertEqual(logs.parse_auditd(path), {
            "type": "PATH", "event": "audit(1794247183.459:599)",
            "time": 1794247183.459, "item": 1,
            "name": "/etc/ssh/sshd_config.d/zz-prueba.conf",
            "nametype": "DELETE",
        })

    def test_the_enriched_half_is_not_read(self):
        """``AUID="root"`` after the 0x1D does not stand for ``auid=``."""
        line = (
            "type=SYSCALL msg=audit(1794247190.000:700): arch=c000003e "
            'syscall=257 success=yes comm="touch" exe="/usr/bin/touch" '
            'key="sudoers_changes"\x1dauid=0 AUID="root"'
        )
        self.assertIsNone(logs.parse_auditd(line)["auid"])

    def test_ipv6_and_port(self):
        """IPv6 is normalized; an impossible port or address is no record."""
        self.assertEqual(logs.parse_sshd(
            "Accepted publickey for deploy from 2001:db8:0:0::5 port 2222 "
            f"ssh2: ED25519 {PANEL_FP}"
        )["ip"], "2001:db8::5")
        record = logs.parse_sshd(
            "Connection closed by invalid user admin 2001:db8::7 port 41000 "
            "[preauth]"
        )
        self.assertEqual(
            (record["user"], record["ip"], record["port"]),
            ("admin", "2001:db8::7", 41000),
        )
        self.assertIsNone(logs.parse_sshd(
            "Connection closed by invalid user admin 172.31.0.2 port 70000 "
            "[preauth]"
        ))
        self.assertIsNone(logs.parse_sshd(
            "Accepted password for root from 300.1.1.1 port 22 ssh2"
        ))

    def test_other_endings_and_logins(self):
        """The other endings, an empty user, a password and a certificate."""
        record = logs.parse_sshd(
            "Disconnecting invalid user oracle 172.31.0.9 port 40001: Too "
            "many authentication failures [preauth]"
        )
        self.assertEqual((record["user"], record["ip"]), ("oracle", "172.31.0.9"))
        record = logs.parse_sshd(
            "Connection closed by invalid user  172.31.0.3 port 53010 [preauth]"
        )
        self.assertEqual((record["user"], record["ip"]), ("", "172.31.0.3"))
        self.assertEqual(logs.parse_sshd(
            "Accepted password for root from 172.31.0.4 port 50000 ssh2"
        ), {
            "kind": "accepted", "user": "root", "ip": "172.31.0.4",
            "port": 50000, "method": "password", "key_type": None,
            "fingerprint": None,
        })
        # A certificate names the key first and its authority after.
        self.assertEqual(logs.parse_sshd(
            "Accepted publickey for root from 172.31.0.5 port 50001 ssh2: "
            f"ED25519-CERT {OTHER_FP} ID ops (serial 7) CA ED25519 {PANEL_FP}"
        )["fingerprint"], OTHER_FP)


class TestHostileLines(BaseCase):
    """What the attacker writes neither passes for a login nor survives."""

    def test_a_user_that_reads_like_a_login_is_no_login(self):
        """A user tried that writes ``Accepted publickey for root`` is a failure."""
        fake = f"Accepted publickey for root from 198.51.100.9 port 22 ssh2: ED25519 {PANEL_FP}"
        self.assertIsNone(logs.parse_sshd(f"Invalid user {fake} from 172.31.0.2 port 53002"))
        record = logs.parse_sshd(
            f"Connection closed by invalid user {fake} 172.31.0.2 port 53002 [preauth]"
        )
        self.assertEqual(record["kind"], "failed")
        self.assertEqual(record["ip"], "172.31.0.2")
        self.assertEqual(record["port"], 53002)
        self.assertLessEqual(len(record["user"]), logs.USER_MAX)
        self.assertNotIn(" ", record["user"])

    def test_an_address_in_the_user_is_not_the_address(self):
        """The address is the last on the line, the one sshd writes."""
        record = logs.parse_sshd(
            "Connection closed by invalid user x 198.51.100.66 port 7 [preauth] "
            "172.31.0.2 port 53002 [preauth]"
        )
        self.assertEqual((record["ip"], record["port"]), ("172.31.0.2", 53002))

    def test_control_characters_and_markdown_are_sanitized(self):
        """Escapes, Markdown and HTML become ``?`` in users and programs."""
        record = logs.parse_sshd(
            "Connection closed by invalid user *ad\x1b[31mmin*_`[x](http://e)` "
            "172.31.0.2 port 53002 [preauth]"
        )
        self.assertEqual(record["user"], "?ad??31mmin?_??x??http???e??")
        record = logs.parse_sudo(
            "b\x07ob* : PWD=/tmp ; USER=root ; COMMAND=/tmp/<b>x</b>\n`id`"
        )
        self.assertEqual(record["user"], "b?ob?")
        self.assertEqual(record["program"], "/tmp/?b?x?/b?")
        for value in (record["user"], record["program"]):
            self.assertRegex(value, r"^[A-Za-z0-9._/+?-]*$")

    def test_lengths_are_capped(self):
        """A user keeps 32 characters and a program 64."""
        record = logs.parse_sshd(
            f"Connection closed by invalid user {'a' * 300} 172.31.0.2 port 1 [preauth]"
        )
        self.assertEqual(record["user"], "a" * logs.USER_MAX)
        record = logs.parse_sudo(
            f"ops : PWD=/ ; USER=root ; COMMAND=/{'b' * 300} --flag"
        )
        self.assertEqual(len(record["program"]), logs.PROGRAM_MAX)

    def test_sudo_keeps_no_argument(self):
        """Arguments can carry secrets: the record has the program alone."""
        line = (
            "ops : TTY=pts/0 ; PWD=/root ; USER=root ; COMMAND=/usr/bin/mysql "
            "-uroot -pS3cr3t-Value --execute=select"
        )
        record = logs.parse_sudo(line)
        self.assertEqual(record["program"], "/usr/bin/mysql")
        self.assertEqual(record["user"], "ops")
        for value in record.values():
            self.assertNotIn("S3cr3t", str(value))

    def test_a_forged_split_is_ambiguous(self):
        """A directory or an argument that writes ``USER=``/``COMMAND=``."""
        for line in (
            "bob : PWD=/tmp/a ; USER=root ; COMMAND=/usr/bin/true ; "
            "USER=nobody ; COMMAND=/tmp/evil",
            "bob : PWD=/tmp ; USER=root ; COMMAND=/tmp/evil ; COMMAND=/usr/bin/true",
        ):
            record = logs.parse_sudo(line)
            self.assertTrue(record["ambiguous"], line)
            self.assertIsNone(record["program"])
            self.assertIsNone(record["run_as"])

    def test_auditd_hex_and_names_are_sanitized(self):
        """auditd's hex strings are decoded, then sanitized."""
        comm = "x y\n*".encode().hex().upper()
        name = '/etc/sudoers.d/a "b"\n`x`'.encode().hex().upper()
        call = logs.parse_auditd(
            "type=SYSCALL msg=audit(1794247190.000:701): auid=1001 uid=0 "
            f'comm={comm} exe="/usr/bin/tee" key="sudoers_changes"'
        )
        self.assertEqual(call["comm"], "x?y??")
        path = logs.parse_auditd(
            f"type=PATH msg=audit(1794247190.000:701): item=1 name={name} "
            "nametype=CREATE"
        )
        self.assertEqual(path["name"], "/etc/sudoers.d/a??b???x?")

    def test_unanchored_shapes_are_not_records(self):
        """A known shape that does not start the line is none."""
        self.assertIsNone(logs.parse_sshd(
            f"x Accepted publickey for root from 1.2.3.4 port 1 ssh2: ED25519 {PANEL_FP}"
        ))
        self.assertIsNone(logs.parse_auditd(
            'node=x type=SYSCALL msg=audit(1794247190.000:1): auid=0 key="passwd_changes"'
        ))
        self.assertIsNone(logs.parse_fail2ban("[sshd] Ban 172.31.2.1"))


class TestAuditJoin(BaseCase):
    """``SYSCALL`` and ``PATH`` join by server and ``audit(…)`` stamp."""

    END = 1794247300 * SECOND

    def _call(self, serial, auid=0):
        """Return a SYSCALL line of the sudoers key with this *serial*."""
        return (
            f"type=SYSCALL msg=audit(1794247200.000:{serial}): arch=c000003e "
            f"syscall=257 success=yes items=2 ppid=1 pid=9 auid={auid} uid=0 "
            'comm="tee" exe="/usr/bin/tee" key="sudoers_changes"'
        )

    def _path(self, serial, item, name, nametype):
        """Return the PATH line *item* of the change *serial*."""
        return (
            f"type=PATH msg=audit(1794247200.000:{serial}): item={item} "
            f'name="{name}" nametype={nametype}'
        )

    def _line(self, host_id, ns, line):
        """Return *line* as Loki gives it for server *host_id* at *ns*."""
        return logs.LogLine(host_id, ns, line, "auditd")

    def test_a_change_takes_its_file(self):
        """The file is the PATH that is not the parent directory."""
        early = self.END - 120 * SECOND
        joined = logs.join_audit([
            self._line(4, early, self._call(800)),
            self._line(4, early, self._path(800, 0, "/etc/sudoers.d/", "PARENT")),
            self._line(4, early, self._path(800, 1, "/etc/sudoers.d/90-x", "CREATE")),
        ], self.END)
        self.assertEqual(joined.deferred, [])
        (event,) = joined.events
        self.assertEqual(event["host_id"], 4)
        self.assertEqual(event["event"], "audit(1794247200.000:800)")
        self.assertEqual(event["file"], "/etc/sudoers.d/90-x")
        self.assertEqual([p["item"] for p in event["paths"]], [0, 1])

    def test_a_path_without_its_syscall_is_left_out(self):
        """A PATH alone is no change."""
        joined = logs.join_audit([
            self._line(4, self.END - 300 * SECOND,
                       self._path(801, 1, "/etc/shadow", "NORMAL")),
        ], self.END)
        self.assertEqual(joined, logs.AuditJoin([], []))

    def test_a_late_syscall_without_path_waits_and_an_old_one_does_not(self):
        """Within 60 s of the end it waits; older, its file is unknown."""
        late = self._line(4, self.END - 30 * SECOND, self._call(802))
        old = self._line(4, self.END - 90 * SECOND, self._call(803))
        joined = logs.join_audit([late, old], self.END)
        self.assertEqual(joined.deferred, [(4, "audit(1794247200.000:802)")])
        (event,) = joined.events
        self.assertEqual(event["event"], "audit(1794247200.000:803)")
        self.assertIsNone(event["file"])
        self.assertEqual(event["paths"], [])

    def test_the_identity_is_the_stamp_not_the_line(self):
        """Read twice, at two Loki instants, it is one change; on two servers, two."""
        ns = self.END - 200 * SECOND
        joined = logs.join_audit([
            self._line(4, ns, self._call(804)),
            self._line(4, ns + 5 * SECOND, self._call(804)),
            self._line(5, ns, self._call(804)),
            self._line(4, ns, self._path(804, 1, "/etc/passwd", "NORMAL")),
            self._line(5, ns, self._path(804, 1, "/etc/group", "NORMAL")),
        ], self.END)
        self.assertEqual(
            sorted((e["host_id"], e["file"]) for e in joined.events),
            [(4, "/etc/passwd"), (5, "/etc/group")],
        )

    def test_the_real_lines_join(self):
        """F0's removal joins its PATH; useradd's, never sent, stays unknown."""
        useradd, panel_touch, _system_touch, path = _fixture("auditd.txt")
        rm_call = (
            "type=SYSCALL msg=audit(1794247183.459:599): arch=c000003e "
            "syscall=263 success=yes exit=0 items=2 ppid=8590 pid=8676 auid=0 "
            'uid=0 comm="rm" exe="/usr/bin/rm" key="ssh_config_changes"'
        )
        ns = self.END - 600 * SECOND
        joined = logs.join_audit(
            [self._line(4, ns, line) for line in (useradd, panel_touch, rm_call, path)],
            self.END,
        )
        by_event = {e["event"]: e for e in joined.events}
        self.assertEqual(
            by_event["audit(1794247183.459:599)"]["file"],
            "/etc/ssh/sshd_config.d/zz-prueba.conf",
        )
        self.assertIsNone(by_event["audit(1794247181.251:525)"]["file"])


class TestTemplates(BaseCase):
    """The panel's LogQL is closed: names and integer ids."""

    def test_one_host_and_several(self):
        """One host is an equality, several a sorted alternation."""
        self.assertEqual(
            logs.logql_lines("accepted", [3]),
            '{log_type="security", source="sshd", host_id="3"} |~ "^Accepted "',
        )
        self.assertIn('host_id=~"1|3"', logs.logql_lines("auditd", [3, 1, 3]))

    def test_nothing_else_gets_in(self):
        """Only positive integers and the known names make LogQL."""
        for bad in ([], None, ["1"], ['1"} or {x="'], [True], [0], [-2], [1.5]):
            with self.assertRaises(ValueError, msg=bad):
                logs.logql_lines("lines", bad)
        with self.assertRaises(KeyError):
            logs.logql_lines('{job=~".+"}', [1])


class TestReading(TransactionCase):
    """LogQL over the account's ``/lr`` route, paged by time."""

    def _get(self, *answers):
        """Patch the transport with *answers*, the busy pause skipped."""
        get = patch(f"{_TRANSPORT}.requests.get", side_effect=_responses(*answers))
        sleep = patch(f"{_TRANSPORT}.time.sleep")
        mocked = get.start()
        sleep.start()
        self.addCleanup(get.stop)
        self.addCleanup(sleep.stop)
        return mocked

    def test_the_log_route_beside_the_read_route(self):
        """``/r`` becomes ``/lr/loki``, with or without a final slash."""
        for url in ("https://central.example.test/r",
                    "https://central.example.test/r/",
                    " https://central.example.test/r// "):
            self.assertEqual(logs.loki_base(url), BASE, url)
        for url in ("", None, "https://vm.example.test:8428",
                    "https://central.example.test/router"):
            self.assertIsNone(logs.loki_base(url), url)

    def test_a_query_reaches_loki_with_the_account(self):
        """The URL, credential, timeout, limit and direction sent."""
        mocked = self._get(_streams((7, 5, "Accepted password for root from "
                                     "172.31.0.4 port 50000 ssh2", "sshd")))
        lines = logs.logql_query_range(
            logs.loki_base("https://central.example.test/r/"), "{x}", 1, 9,
            token="t", user="acct",
        )
        self.assertEqual(lines, [logs.LogLine(
            7, 5, "Accepted password for root from 172.31.0.4 port 50000 ssh2",
            "sshd",
        )])
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], f"{BASE}/api/v1/query_range")
        self.assertEqual(kwargs["auth"], ("acct", "t"))
        self.assertEqual(kwargs["timeout"], logs.QUERY_TIMEOUT)
        self.assertEqual(kwargs["params"], {
            "query": "{x}", "start": 1, "end": 9, "limit": logs.PAGE_LIMIT,
            "direction": "forward",
        })

    def test_pages_follow_from_the_last_instant_included(self):
        """A line on both pages counts once; the next page starts at it."""
        mocked = self._get(
            _streams((4, 10, "a"), (4, 20, "b"), (4, 30, "c")),
            _streams((4, 30, "c"), (4, 30, "d"), (4, 40, "e")),
            _streams((4, 40, "e")),
        )
        read = logs.read_lines(BASE, "{x}", 0, 100, limit=3)
        self.assertEqual([line.line for line in read.lines], ["a", "b", "c", "d", "e"])
        self.assertEqual(read.last_ns, 40)
        self.assertTrue(read.complete)
        starts = [call.kwargs["params"]["start"] for call in mocked.call_args_list]
        self.assertEqual(starts, [0, 30, 40])

    def test_the_same_text_on_two_servers_is_two_lines(self):
        """The identity of a line includes its server."""
        self._get(_streams((4, 10, "x"), (5, 10, "x")))
        read = logs.read_lines(BASE, "{x}", 0, 100, limit=3)
        self.assertEqual([(line.host_id, line.line) for line in read.lines], [(4, "x"), (5, "x")])

    def test_the_page_limit_stops_the_read_and_says_where(self):
        """At the page limit the read is incomplete, up to its last line."""
        self._get(
            _streams((4, 10, "a"), (4, 20, "b")),
            _streams((4, 20, "b"), (4, 30, "c")),
        )
        read = logs.read_lines(BASE, "{x}", 0, 100, limit=2, max_pages=2)
        self.assertFalse(read.complete)
        self.assertEqual(read.last_ns, 30)

    def test_a_full_instant_moves_on(self):
        """More lines at one instant than a page holds cannot loop forever."""
        mocked = self._get(
            _streams((4, 10, "a"), (4, 10, "b")),
            _streams((4, 10, "a"), (4, 10, "b")),
            _streams((4, 12, "c")),
        )
        with self.assertLogs("odoo.addons.incubacloud.models.logs", "WARNING"):
            read = logs.read_lines(BASE, "{x}", 0, 100, limit=2)
        self.assertEqual([line.line for line in read.lines], ["a", "b", "c"])
        self.assertEqual(mocked.call_args_list[2].kwargs["params"]["start"], 11)

    def test_syscall_and_path_on_two_pages_join(self):
        """Every page is read before the join, so a split change joins."""
        call = (
            "type=SYSCALL msg=audit(1794247200.000:900): auid=1001 uid=0 "
            'comm="tee" exe="/usr/bin/tee" key="sudoers_changes"'
        )
        path = (
            "type=PATH msg=audit(1794247200.000:900): item=1 "
            'name="/etc/sudoers.d/90-x" nametype=CREATE'
        )
        end = 1000 * SECOND
        self._get(
            _streams((4, 100 * SECOND, "type=SYSCALL x"), (4, 200 * SECOND, call)),
            _streams((4, 200 * SECOND, call), (4, 200 * SECOND + 1, path)),
            _streams((4, 200 * SECOND + 1, path)),
        )
        read = logs.read_lines(BASE, "{x}", 0, end, limit=2)
        (event,) = logs.join_audit(read.lines, end).events
        self.assertEqual(event["file"], "/etc/sudoers.d/90-x")
        self.assertEqual(event["auid"], 1001)

    def test_a_long_window_is_read_in_tranches_newest_first(self):
        """No request spans more than a tranche; the window is read whole.

        Loki splits a request into subqueries and refuses one whose
        subqueries fill its queue (429): seven days in one request were
        refused by Loki 2.9 as it comes (QA of 15-nov-2026).
        """
        tranche = logs.TRANCHE_SECONDS * SECOND
        end = 2 * tranche + 10 * SECOND
        mocked = self._get(
            _streams((4, end - 5 * SECOND, "c")),
            _streams((4, tranche + SECOND, "b")),
            _streams((4, 3 * SECOND, "a")),
        )
        lines, cut = logs.read_last_lines(BASE, "{x}", 0, end, pages=1, limit=3)
        self.assertEqual([line.line for line in lines], ["a", "b", "c"])
        self.assertFalse(cut)
        windows = [(call.kwargs["params"]["start"], call.kwargs["params"]["end"])
                   for call in mocked.call_args_list]
        self.assertEqual(windows, [
            (end - tranche, end), (end - 2 * tranche, end - tranche),
            (0, end - 2 * tranche),
        ])

    def test_the_sample_is_shared_by_the_tranches(self):
        """The pages and lines of a sample are of the whole window, not of each tranche."""
        tranche = logs.TRANCHE_SECONDS * SECOND
        end = 3 * tranche
        mocked = self._get(
            _streams((4, end - 2, "e"), (4, end - 1, "f")),
            _streams((4, end - tranche - 3, "b"), (4, end - tranche - 2, "c"),
                     (4, end - tranche - 1, "d")),
            _streams((4, end - tranche - 4, "a")),
        )
        lines, cut = logs.read_last_lines(BASE, "{x}", 0, end, pages=2, limit=3)
        self.assertEqual([line.line for line in lines], ["a", "b", "c", "d", "e", "f"])
        self.assertTrue(cut)
        self.assertEqual(
            [call.kwargs["params"]["start"] for call in mocked.call_args_list],
            [end - tranche, end - 2 * tranche, end - 2 * tranche],
        )

    def test_a_busy_central_is_no_outage(self):
        """A 429 is asked once more, then raised as busy: no alert."""
        alerts = self.env["cloud.alert"].sudo()
        before = alerts.search_count([("code", "=", BACKEND_UNREACHABLE_CODE)])
        mocked = self._get(429, 429)
        with self.assertRaises(requests.HTTPError) as caught:
            logs.read_lines(BASE, "{x}", 0, 100)
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(central_busy(caught.exception))
        self.assertEqual(
            alerts.search_count([("code", "=", BACKEND_UNREACHABLE_CODE)]), before,
        )

    def test_a_failure_on_a_later_page_returns_nothing(self):
        """A window read in part never passes for one read whole."""
        self._get(
            _streams((4, 10, "a"), (4, 20, "b")),
            requests.ConnectionError("reset"), requests.ConnectionError("reset"),
        )
        with self.assertRaises(requests.ConnectionError):
            logs.read_lines(BASE, "{x}", 0, 100, limit=2)

    def test_a_loki_error_is_raised(self):
        """An error answer raises, it is not an empty window."""
        self._get({"status": "error", "error": "parse error"})
        with self.assertRaises(ValueError):
            logs.logql_query_range(BASE, "{x", 0, 100)

    def test_a_metric_query(self):
        """An instant LogQL aggregation gives labels and values."""
        mocked = self._get({"status": "success", "data": {
            "resultType": "vector",
            "result": [{"metric": {"host_id": "4"}, "value": [1, "17"]}],
        }})
        self.assertEqual(
            logs.logql_query(BASE, "sum(x)", at_ns=5),
            [({"host_id": "4"}, 17.0)],
        )
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], f"{BASE}/api/v1/query")
        self.assertEqual(kwargs["params"], {"query": "sum(x)", "time": 5})


def _weblogin(ip, instance=42, t="2026-11-14T10:00:01Z"):
    """Return the line a server sends for one failed web login (C1.9).

    :rtype: str
    """
    return f"weblogin failed ip={ip} instance={instance} t={t}"


class TestWebloginLines(BaseCase):
    """The instances' failed web logins: the server's own line, whole."""

    def test_a_line_gives_address_instance_and_time(self):
        """IPv4, IPv6 and the proxy's time with a fraction and an offset."""
        self.assertEqual(logs.parse_weblogin(_weblogin("203.0.113.5")), {
            "ip": "203.0.113.5", "instance_id": 42,
            "time": "2026-11-14T10:00:01Z",
        })
        self.assertEqual(logs.parse_weblogin(_weblogin(
            "2001:DB8::7", instance=0, t="2026-11-14T10:00:01.123456789+01:00",
        )), {
            "ip": "2001:db8::7", "instance_id": 0,
            "time": "2026-11-14T10:00:01.123456789+01:00",
        })

    def test_anything_else_is_no_line(self):
        """Text after it or before it, a bad address or id: ignored."""
        for line in (
            _weblogin("203.0.113.5") + " extra",
            _weblogin("203.0.113.5") + "\n",
            "x " + _weblogin("203.0.113.5"),
            _weblogin("999.0.113.5"),
            _weblogin("203.0.113"),
            _weblogin("::g"),
            _weblogin("203.0.113.5", instance="4a"),
            _weblogin("203.0.113.5", instance="-4"),
            _weblogin("203.0.113.5", t="yesterday"),
            "weblogin failed ip=203.0.113.5 instance=42",
            "", None,
        ):
            self.assertIsNone(logs.parse_weblogin(line), line)

    def test_ipv6_counts_by_its_64(self):
        """Two addresses of one /64 are one source; IPv4 is itself."""
        self.assertEqual(logs.source_key("2001:db8:1:2::5"), "2001:db8:1:2::/64")
        self.assertEqual(
            logs.source_key("2001:db8:1:2:ffff::9"), "2001:db8:1:2::/64",
        )
        self.assertEqual(logs.source_key("203.0.113.5"), "203.0.113.5")

    def test_the_query_of_one_instance(self):
        """One server by id, the instance by a filter that 4 is not 42."""
        self.assertEqual(
            logs.logql_weblogin(3, 42),
            '{log_type="security", source="weblogin", host_id="3"}'
            ' |= " instance=42 "',
        )
        for bad in ("42", -1, True, 4.2, None):
            with self.assertRaises(ValueError, msg=bad):
                logs.logql_weblogin(3, bad)
        with self.assertRaises(ValueError):
            logs.logql_weblogin(0, 42)


class TestWebloginTopSources(TransactionCase):
    """The addresses with the most attempts, counted on the panel."""

    def setUp(self):
        """A reader of the central, the transport patched by each test."""
        super().setUp()
        self.reader = logs.LogReader(BASE, token="t", user="acct")
        patcher = patch.object(logs, "PAGE_LIMIT", 3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, *answers):
        """Patch the transport with *answers*, the busy pause skipped."""
        get = patch(f"{_TRANSPORT}.requests.get", side_effect=_responses(*answers))
        sleep = patch(f"{_TRANSPORT}.time.sleep")
        mocked = get.start()
        sleep.start()
        self.addCleanup(get.stop)
        self.addCleanup(sleep.stop)
        return mocked

    def test_two_pages_back_without_repeats(self):
        """The newest page first, the next ending at its first instant."""
        mocked = self._get(
            _streams((7, 40, _weblogin("203.0.113.5"), "weblogin"),
                     (7, 50, _weblogin("198.51.100.7"), "weblogin"),
                     (7, 60, _weblogin("203.0.113.5"), "weblogin")),
            _streams((7, 20, _weblogin("203.0.113.5"), "weblogin"),
                     (7, 30, _weblogin("203.0.113.9"), "weblogin"),
                     (7, 40, _weblogin("203.0.113.5"), "weblogin")),
        )
        got = self.reader.weblogin_sources(7, 42, minutes=10, now_ns=1000)
        self.assertEqual(got, logs.WebloginSources(
            [("203.0.113.5", 3), ("198.51.100.7", 1), ("203.0.113.9", 1)],
            3, 5, True,
        ))
        first, second = (call.kwargs["params"] for call in mocked.call_args_list)
        self.assertEqual(first["query"], logs.logql_weblogin(7, 42))
        self.assertEqual(first["direction"], "backward")
        self.assertEqual(first["end"], 1001)
        self.assertEqual(first["start"], 1000 - 600 * SECOND)
        self.assertEqual(second["end"], 41)
        self.assertEqual(mocked.call_args.kwargs["auth"], ("acct", "t"))

    def test_ipv6_by_64_ordered_and_not_cut(self):
        """A /64 is one source; a short page means the window was read whole."""
        self._get(_streams(
            (7, 10, _weblogin("2001:db8:1:2::5"), "weblogin"),
            (7, 11, _weblogin("2001:db8:1:2::6"), "weblogin"),
        ))
        got = self.reader.weblogin_sources(7, 42, now_ns=1000)
        self.assertEqual(got.top, [("2001:db8:1:2::/64", 2)])
        self.assertEqual((got.distinct, got.attempts, got.cut), (1, 2, False))

    def test_other_lines_do_not_count(self):
        """A line of another instance or of another shape is not an attempt."""
        self._get(_streams(
            (7, 10, _weblogin("203.0.113.5", instance=4), "weblogin"),
            (7, 11, _weblogin("203.0.113.6") + " ip=9.9.9.9", "weblogin"),
        ))
        got = self.reader.weblogin_sources(7, 42, now_ns=1000)
        self.assertEqual(got, logs.WebloginSources([], 0, 0, False))

    def test_no_answer_is_no_data(self):
        """Busy, down, an error or a bad id: None, and nothing raised."""
        for answers in (
            (429, 429),
            (requests.ConnectionError("reset"), requests.ConnectionError("reset")),
            (requests.Timeout("slow"), requests.Timeout("slow")),
            ({"status": "error", "error": "too many"},),
        ):
            with self.subTest(answers=answers):
                self._get(*answers)
                with self.assertLogs(
                    "odoo.addons.incubacloud.models.logs", "WARNING",
                ):
                    self.assertIsNone(
                        self.reader.weblogin_sources(7, 42, now_ns=1000),
                    )
        with self.assertLogs("odoo.addons.incubacloud.models.logs", "WARNING"):
            self.assertIsNone(self.reader.weblogin_sources(7, "42"))

    def test_the_pairs_the_rule_names(self):
        """From the metrics URL, pairs most first; nothing read is None."""
        mocked = self._get(_streams(
            (7, 10, _weblogin("203.0.113.5"), "weblogin"),
            (7, 11, _weblogin("2001:db8:1:2::6"), "weblogin"),
            (7, 12, _weblogin("203.0.113.5"), "weblogin"),
        ), _streams(), _streams(), 429, 429)
        self.assertEqual(
            logs.weblogin_top_sources(
                "https://central.example.test/r", 7, 42, minutes=10,
                token="t", user="acct", now_ns=1000,
            ),
            [("203.0.113.5", 2), ("2001:db8:1:2::/64", 1)],
        )
        self.assertEqual(mocked.call_args.args[0], f"{BASE}/api/v1/query_range")
        self.assertIsNone(logs.weblogin_top_sources(BASE, 7, 42, now_ns=1000))
        with self.assertLogs("odoo.addons.incubacloud.models.logs", "WARNING"):
            self.assertIsNone(logs.weblogin_top_sources(BASE, 7, 42, now_ns=1000))
        self.assertIsNone(
            logs.weblogin_top_sources("https://vm.example.test:8428", 7, 42),
        )

    def test_the_reader_of_the_settings(self):
        """From the panel's central and credential; none without a central."""
        settings = self.env["cloud.settings"].sudo()._get_system()
        settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "https://central.example.test/r",
            "metrics_account": "acct",
            "metrics_remote_write_token": "secret",
        })
        reader = logs.LogReader.from_env(self.env)
        self.assertEqual(
            (reader.base_url, reader.user, reader.token),
            (BASE, "acct", "secret"),
        )
        settings.metrics_central_url = "https://vm.example.test:8428"
        self.assertIsNone(logs.LogReader.from_env(self.env))
        settings.write({
            "metrics_central_url": "https://central.example.test/r",
            "metrics_enabled": False,
        })
        self.assertIsNone(logs.LogReader.from_env(self.env))
