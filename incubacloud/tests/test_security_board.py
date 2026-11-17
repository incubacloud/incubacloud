"""A server's Security view: ``/cloud/host/security_board`` and ``security_lines``.

Plan #84, cut C6, decisions D3 and D4: the figures for the Developer role
and above, the tables with addresses, users and commands, and "see
lines", only for whoever manages servers, decided by the route and not by
the screen; ids and ranges from the browser, never LogQL; nothing of a
server the caller cannot read. Driven over real HTTP, with the central
replaced at ``requests.get`` (its PromQL and its Loki alike) and the
country lookup by an autospec.
"""
import json
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import create_autospec, patch

from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models import geoip, logs, metrics_security
from ..models.metrics_charts import PANELS
from ..models.logs import LogLine
from ..models.metrics_diagnosis import diagnose_host_security
from .test_geoip import _mmdb
from .test_host_fleet_series import _ROLES, _matrix, _response, _vector

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"
_SECOND = 1_000_000_000

#: Addresses and users the central's lines carry: none may reach a
#: Developer's answer.
_PRIVATE = (
    "203.0.113.5", "198.51.100.7", "2001:db8:1:2", "172.31.0.2",
    "invitado", "probador", "/usr/bin/true", "203.0.113.10",
)


def _streams(source, lines):
    """Loki's answer to a line query: one stream of *source*.

    :param str source: the ``source`` label.
    :param list lines: ``(ns, line)``.
    """
    return _response({"status": "success", "data": {
        "resultType": "streams",
        "result": [{
            "stream": {"log_type": "security", "source": source, "host_id": "1"},
            "values": [[str(ns), line] for ns, line in lines],
        }] if lines else [],
    }})


def _f2b(ns, jail, action, ip):
    """A fail2ban line as Loki holds it."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ns / _SECOND))
    return (ns, f"{stamp},000 fail2ban.actions        [6563]: NOTICE  [{jail}] {action} {ip}")


class _Central:
    """The central of a server with a web-login attack and some SSH noise.

    Answers PromQL (the counters) and Loki (the lines) from what
    ``requests.get`` is asked, and keeps the Loki queries it was sent.
    """

    def __init__(self, instance, stranger, full_failed=False):
        """Lines about *instance* (of the server) and *stranger* (of another).

        :param bool full_failed: answer the failed SSH read with full
            pages, so its sample is cut.
        """
        now = time.time_ns()
        self.loki = []
        mine, theirs = instance.id, stranger.id
        web = [
            (now - 300 * _SECOND, f"weblogin failed ip=203.0.113.5 instance={mine} t=2026-11-14T10:00:00Z"),
            (now - 290 * _SECOND, f"weblogin failed ip=203.0.113.5 instance={mine} t=2026-11-14T10:00:10Z"),
            (now - 280 * _SECOND, f"weblogin failed ip=203.0.113.5 instance={mine} t=2026-11-14T10:00:20Z"),
            (now - 270 * _SECOND, f"weblogin failed ip=198.51.100.7 instance={theirs} t=2026-11-14T10:00:30Z"),
            (now - 260 * _SECOND, f"weblogin failed ip=2001:db8:1:2::7 instance=0 t=2026-11-14T10:00:40Z"),
        ]
        bans = [
            _f2b(now - 250 * _SECOND, "incubacloud-weblogin", "Ban", "203.0.113.5"),
            _f2b(now - 240 * _SECOND, "sshd", "Ban", "172.31.0.2"),
            _f2b(now - 230 * _SECOND, "sshd", "Unban", "172.31.0.2"),
        ]
        failed = [
            (now - 200 * _SECOND, "Connection closed by invalid user invitado 172.31.0.2 port 53002 [preauth]"),
            (now - 190 * _SECOND, "Connection closed by authenticating user root 172.31.0.2 port 53008 [preauth]"),
        ]
        if full_failed:
            failed = [
                (now - (1000 - n) * _SECOND,
                 f"Connection closed by authenticating user root 172.31.0.{n % 200 + 1} port 5{n:04d} [preauth]")
                for n in range(500)
            ]
        self.lines = {
            "weblogin": web,
            "fail2ban": bans,
            "accepted": [(now - 100 * _SECOND, "Accepted password for probador from 203.0.113.10 port 60560 ssh2")],
            "failed": failed,
            "sudo": [(now - 90 * _SECOND, "probador : PWD=/home/probador ; USER=root ; COMMAND=/usr/bin/true")],
            "auditd": [],
        }
        self.instance, self.stranger = instance, stranger

    def _loki(self, params):
        """The lines of the query asked."""
        query = params["query"]
        self.loki.append(params)
        if 'source="weblogin"' in query:
            return _streams("weblogin", self.lines["weblogin"])
        if 'source="fail2ban"' in query:
            return _streams("fail2ban", self.lines["fail2ban"])
        if 'source="sshd"' in query:
            key = "accepted" if "Accepted" in query else "failed"
            return _streams("sshd", self.lines[key])
        if 'source="sudo"' in query:
            return _streams("sudo", self.lines["sudo"])
        return _streams("auditd", self.lines["auditd"])

    def __call__(self, url, params=None, **kw):
        """Answer one GET of the panel."""
        if "/lr/loki/" in url:
            return self._loki(params)
        roles = _ROLES.findall(params["query"])
        if "step" in params:
            return _matrix(params, [({"ic_role": role, "instance_id": str(self.instance.id)}, 1)
                                    for role in roles])
        if "ssh_failed_10m" in roles:
            return _vector([
                ({"ic_role": "ssh_failed_10m"}, 12), ({"ic_role": "bans_1h"}, 1),
                ({"ic_role": "collector_up"}, 1), ({"ic_role": "lines_15m"}, 40),
                ({"ic_role": "weblogin_bans"}, 1), ({"ic_role": "weblogin_unbans"}, 0),
            ])
        mine, theirs = str(self.instance.id), str(self.stranger.id)
        return _vector([
            ({"instance_id": mine, "ic_role": "rps"}, 3.2),
            ({"instance_id": mine, "ic_role": "weblogin"}, 41),
            ({"instance_id": theirs, "ic_role": "weblogin"}, 5),
            ({"instance_id": "0", "ic_role": "weblogin"}, 7),
        ])


@tagged("post_install", "-at_install")
class _SecurityBoardCase(HttpCase):
    """A server whose Security view is asked for, and who asks."""

    def setUp(self):
        """A server with security logs, one instance on it, one elsewhere."""
        super().setUp()
        metrics_security._CACHE.clear()
        self.env["cloud.settings"].sudo()._get_system().write({
            "metrics_enabled": True,
            "metrics_central_url": "https://central.test/r",
            "metrics_account": "acct_test",
            "metrics_remote_write_token": "s3cr3t",
        })
        project = self.env["cloud.project"].create({"name": "SecBoard"})
        self.host = self.env["cloud.host"].create({
            "name": "HSecBoard", "ip_address": "203.0.113.81", "user": "root",
            "wildcard_domain": "hsecboard.example.com",
        })
        # The board where this panel may receive security logs: a layer
        # that withholds them until it is allowed (its own tests) is
        # allowed here; the withheld state has a test of its own.
        available = patch.object(
            type(self.env["cloud.host"]), "_security_logs_available",
            autospec=True, return_value=True,
        )
        self.available = available.start()
        self.addCleanup(available.stop)
        self.host.security_logs_enabled = True
        other = self.env["cloud.host"].create({
            "name": "HSecOther", "ip_address": "203.0.113.82", "user": "root",
            "wildcard_domain": "hsecother.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
        })
        self.stranger = self.env["cloud.instance"].create({
            "name": "elsewhere",
            "project_id": self.env["cloud.project"].create({"name": "SecOther"}).id,
            "environment": "production", "host_id": other.id,
        })
        for login, group in (
            ("secb-dev", "incubacloud.group_cloud_developer"),
            ("secb-mgr", "incubacloud.group_cloud_manager"),
            ("secb-user", "incubacloud.group_cloud_user"),
        ):
            self.env["res.users"].create({
                "name": login, "login": login, "password": login,
                "group_ids": [(6, 0, [
                    self.env.ref("base.group_user").id, self.env.ref(group).id,
                ])],
            })
        self.lookup = create_autospec(geoip.CountryLookup, instance=True)
        self.lookup.return_value = "DE"
        # A property on the real class: a value, never a call.
        self.lookup.attribution = geoip.ATTRIBUTION
        country = patch.object(geoip, "resolver", return_value=self.lookup)
        country.start()
        self.addCleanup(country.stop)

    def _call(self, route, **params):
        """POST *route* on the current session and return the JSON body."""
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": params}
        return self.url_open(
            route, data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()

    def _board(self, login, central=None, **params):
        """The board as *login* sees it, and the central that answered."""
        central = central or _Central(self.instance, self.stranger)
        self.authenticate(login, login)
        with patch(f"{_MODULE}.requests.get", side_effect=central):
            res = self._call("/cloud/host/security_board", host_id=self.host.id, **params)
        return res["result"], central


class TestSecurityBoard(_SecurityBoardCase):
    """Who gets what from a server's Security view."""

    def test_a_developer_gets_the_figures_and_no_address_nor_user(self):
        """The JSON itself, not the screen: no table, no address, no user."""
        res, central = self._board("secb-dev", range_key="24h")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["state"], "ready")
        self.assertFalse(res["detail"])
        self.assertNotIn("tables", res)
        text = json.dumps(res)
        for private in _PRIVATE:
            self.assertNotIn(private, text)
        figures = res["weblogin"]
        self.assertEqual(figures["instances"], [
            {"id": self.instance.id, "name": "prod", "attempts": 41},
        ])
        # Another server's instance and instance 0 are "unidentified".
        self.assertEqual(figures["unidentified"], 12)
        self.assertEqual(figures["distinct"], 3)
        self.assertEqual(figures["bans"], 1)
        self.assertEqual(set(res["panels"]), {p["key"] for p in PANELS["host_security"]})
        # Only the web logins' lines were read, for their count.
        self.assertTrue(central.loki)
        for params in central.loki:
            self.assertIn('source="weblogin"', params["query"])
            self.assertIn(f'host_id="{self.host.id}"', params["query"])

    def test_a_manager_gets_the_tables_with_address_country_attempts_and_instances(self):
        """Both web-login tables, the SSH tables, the sign-ins and sudo."""
        res, _central = self._board("secb-mgr", range_key="24h")
        self.assertTrue(res["detail"])
        tables = res["tables"]
        top = tables["weblogin_sources"]["rows"]
        self.assertEqual(top[0]["address"], "203.0.113.5")
        self.assertEqual(top[0]["country"], "DE")
        self.assertEqual(top[0]["attempts"], 3)
        self.assertEqual(top[0]["instances"], [{"id": self.instance.id, "name": "prod", "attempts": 3}])
        # A Ban with no Unban after it: banned now.
        self.assertIs(top[0]["blocked"], True)
        # An instance of another server is never named here.
        stranger = next(r for r in top if r["address"] == "198.51.100.7")
        self.assertEqual(stranger["instances"], [{"id": 0, "name": None, "attempts": 1}])
        self.assertIn("2001:db8:1:2::/64", [r["address"] for r in top])
        bans = tables["weblogin_bans"]["rows"]
        self.assertEqual([b["address"] for b in bans], ["203.0.113.5"])
        self.assertEqual(bans[0]["instances"][0]["name"], "prod")
        ssh = tables["ssh_sources"]
        self.assertEqual(ssh["rows"][0]["address"], "172.31.0.2")
        self.assertEqual(ssh["rows"][0]["attempts"], 2)
        # Banned, then lifted: not banned now.
        self.assertIs(ssh["rows"][0]["blocked"], False)
        self.assertEqual({u["user"] for u in ssh["users"]}, {"invitado", "root"})
        self.assertFalse(ssh["cut"])
        accepted = tables["accepted"]["rows"]
        self.assertEqual(accepted[0]["user"], "probador")
        self.assertEqual(accepted[0]["attribution"], "other_user")
        sudo = tables["sudo"]["rows"]
        self.assertEqual(sudo[0]["program"], "/usr/bin/true")
        self.assertEqual(sudo[0]["attribution"], "other_user")
        self.assertEqual(res["attribution"], geoip.ATTRIBUTION)

    def test_a_real_lookup_with_a_public_address_gives_its_country_and_attribution(self):
        """A real ``CountryLookup``, not the autospec: ``attribution`` is a property.

        A public address of the sign-ins resolves to a country, so the
        attribution is read: DB-IP's line from DB-IP's database, none
        from Odoo's.
        """
        if geoip.maxminddb is None:
            self.skipTest("maxminddb is not installed")
        tmp = tempfile.mkdtemp(prefix="test-secboard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp, "country.mmdb")
        path.write_bytes(_mmdb({"8.8.8.0/24": "US"}))
        reader = geoip.maxminddb.open_database(str(path))
        self.addCleanup(reader.close)
        for source, attribution in (("dbip", geoip.ATTRIBUTION), ("odoo", None)):
            with self.subTest(source=source):
                metrics_security._CACHE.clear()
                central = _Central(self.instance, self.stranger)
                central.lines["accepted"] = [(
                    time.time_ns() - 100 * _SECOND,
                    "Accepted publickey for probador from 8.8.8.8 port 60560 ssh2",
                )]
                lookup = geoip.CountryLookup(reader, source)
                with patch.object(geoip, "resolver", return_value=lookup):
                    res, _central = self._board("secb-mgr", central=central, range_key="24h")
                self.assertTrue(res["ok"], res)
                accepted = res["tables"]["accepted"]["rows"]
                self.assertEqual(accepted[0]["address"], "8.8.8.8")
                self.assertEqual(accepted[0]["country"], "US")
                self.assertEqual(res["attribution"], attribution)

    def test_seven_days_of_an_aggregation_are_cut_to_24_hours(self):
        """The failed SSH sample reaches 24 h; the sign-ins, filtered, 7 days.

        Read in tranches: no request asks Loki for more than one, so a
        central on Loki's own limits answers a week too (QA of
        15-nov-2026, 429 on every 7-day table).
        """
        _res, central = self._board("secb-mgr", range_key="7d")
        reach = {}
        for params in central.loki:
            self.assertLessEqual(
                params["end"] - params["start"],
                (logs.TRANCHE_SECONDS + 1) * _SECOND, params["query"],
            )
            query = params["query"]
            kind = "accepted" if "Accepted" in query else (
                "failed" if 'source="sshd"' in query else None)
            if kind:
                first, last = reach.get(kind, (params["start"], params["end"]))
                reach[kind] = (min(first, params["start"]), max(last, params["end"]))
        spans = {kind: (last - first) / _SECOND for kind, (first, last) in reach.items()}
        self.assertLessEqual(spans["failed"], 24 * 3600 + 1)
        self.assertGreater(spans["accepted"], 6 * 24 * 3600)

    def test_a_full_sample_says_it_counts_the_last_lines(self):
        """More lines than the sample reads: the table says so."""
        central = _Central(self.instance, self.stranger, full_failed=True)
        res, _central = self._board("secb-mgr", central=central)
        ssh = res["tables"]["ssh_sources"]
        self.assertTrue(ssh["cut"])
        self.assertEqual(ssh["sample"], metrics_security.SAMPLE_LINES)

    def test_behind_a_cdn_the_board_says_so(self):
        """The addresses would be the CDN's, and the jail is off."""
        self.host.behind_cdn = True
        res, _central = self._board("secb-dev")
        self.assertTrue(res["behind_cdn"])
        self.assertFalse(res["defense"]["weblogin_jail"])

    def test_the_instance_filter_takes_an_instance_of_this_server_alone(self):
        """An id of another server's instance, or text, is refused unread."""
        for bad in (self.stranger.id, "1 or {}", '{x="y"}', -3):
            res, central = self._board("secb-mgr", instance_id=bad)
            self.assertEqual(res, {"ok": False, "error": "Instance not found"}, bad)
            self.assertFalse(central.loki)
        res, central = self._board("secb-mgr", instance_id=self.instance.id)
        self.assertTrue(res["ok"])
        web = [p["query"] for p in central.loki if 'source="weblogin"' in p["query"]]
        self.assertTrue(web)
        for query in web:
            self.assertIn(f'|= " instance={self.instance.id} "', query)
        self.assertEqual(res["weblogin"]["window_hours"], 7 * 24)

    def test_a_role_below_developer_is_refused(self):
        """No figure either."""
        self.authenticate("secb-user", "secb-user")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/security_board", host_id=self.host.id)
        self.assertIn("error", res)
        get.assert_not_called()

    def test_an_unknown_archived_or_text_host_is_not_found(self):
        """Nothing is asked of the central for it."""
        self.authenticate("secb-mgr", "secb-mgr")
        archived = self.env["cloud.host"].create({
            "name": "HSecGone", "ip_address": "203.0.113.83", "user": "root",
            "wildcard_domain": "hsecgone.example.com", "active": False,
        })
        with patch(f"{_MODULE}.requests.get") as get:
            for host_id in (self.host.id + 10000, archived.id, str(self.host.id)):
                res = self._call("/cloud/host/security_board", host_id=host_id)["result"]
                self.assertEqual(res, {"ok": False, "error": "Host not found"}, host_id)
        get.assert_not_called()

    def test_unavailable_on_this_panel_asks_nothing(self):
        """A managed panel without its manager's permission: the state alone."""
        self.available.return_value = False
        self.authenticate("secb-mgr", "secb-mgr")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/security_board", host_id=self.host.id)["result"]
        self.assertEqual(res["state"], "unavailable")
        get.assert_not_called()

    def test_off_on_this_server_asks_nothing(self):
        """Security logs off: the state and the defence, from the database."""
        self.host.security_logs_enabled = False
        self.authenticate("secb-dev", "secb-dev")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/security_board", host_id=self.host.id)["result"]
        self.assertEqual(res["state"], "off")
        self.assertFalse(res["defense"]["enabled"])
        get.assert_not_called()

    def test_observability_off_is_said_without_asking(self):
        """No central: the boards' own answer."""
        self.env["cloud.settings"].sudo()._get_system().metrics_enabled = False
        self.authenticate("secb-dev", "secb-dev")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/security_board", host_id=self.host.id)["result"]
        self.assertEqual(res["reason"], "disabled")
        get.assert_not_called()

    def test_lines_are_refused_to_a_developer(self):
        """"See lines" is a manager's, refused by the route."""
        self.authenticate("secb-dev", "secb-dev")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/security_lines", host_id=self.host.id, source="sshd")
        self.assertIn("error", res)
        self.assertNotIn("result", res)
        get.assert_not_called()

    def test_a_manager_reads_lines_of_a_closed_kind(self):
        """Newest first, at most 200; a kind that is not one of them reads nothing."""
        central = _Central(self.instance, self.stranger)
        self.authenticate("secb-mgr", "secb-mgr")
        with patch(f"{_MODULE}.requests.get", side_effect=central):
            res = self._call("/cloud/host/security_lines", host_id=self.host.id,
                             source="weblogin", instance_id=self.instance.id)["result"]
            self.assertTrue(res["ok"], res)
            self.assertEqual(len(res["lines"]), 5)
            self.assertLess(res["lines"][1]["at"], res["lines"][0]["at"])
            self.assertEqual(central.loki[-1]["limit"], metrics_security.LINES_MAX)
            self.assertIn(f'|= " instance={self.instance.id} "', central.loki[-1]["query"])
            asked = len(central.loki)
            for bad in ('{job=~".+"}', "lines", None):
                res = self._call("/cloud/host/security_lines", host_id=self.host.id,
                                 source=bad)["result"]
                self.assertFalse(res["ok"], bad)
            self.assertEqual(len(central.loki), asked)


class TestKnownKeysRoutes(_SecurityBoardCase):
    """The known keys' routes and card, over HTTP (design note of 17-nov-2026)."""

    #: A fingerprint of no key anyone holds.
    FINGERPRINT = "SHA256:" + "K" * 43

    def test_a_manager_adds_and_removes_a_developer_reads(self):
        """The routes for whoever manages servers; the card for both."""
        self.authenticate("secb-dev", "secb-dev")
        refused = self._call(
            "/cloud/host/known_key_add", host_id=self.host.id,
            key=self.FINGERPRINT, name="Laptop", networks="192.0.2.15",
        )
        self.assertIn("error", refused)
        self.authenticate("secb-mgr", "secb-mgr")
        added = self._call(
            "/cloud/host/known_key_add", host_id=self.host.id,
            key=self.FINGERPRINT, name="Laptop", networks="192.0.2.15",
        )["result"]
        self.assertTrue(added["ok"], added)
        (row,) = added["known_keys"]["rows"]
        self.assertEqual((row["name"], row["fingerprint"]), ("Laptop", self.FINGERPRINT))
        self.assertEqual(row["allowed_networks"], "192.0.2.15/32")
        bad = self._call(
            "/cloud/host/known_key_add", host_id=self.host.id,
            key="-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE=\n-----END OPENSSH PRIVATE KEY-----",
            name="Oops",
        )["result"]
        self.assertFalse(bad["ok"])
        self.assertNotIn("b3BlbnNzaC1rZXktdjE", bad["error"])
        mine, _central = self._board("secb-mgr", range_key="24h")
        self.assertTrue(mine["known_keys"]["can_edit"])
        theirs, _central = self._board("secb-dev", range_key="24h")
        self.assertFalse(theirs["known_keys"]["can_edit"])
        self.assertNotIn("192.0.2.15", json.dumps(theirs["known_keys"]))
        self.authenticate("secb-dev", "secb-dev")
        refused = self._call(
            "/cloud/host/known_key_remove", host_id=self.host.id, key_id=row["id"],
        )
        self.assertIn("error", refused)
        self.authenticate("secb-mgr", "secb-mgr")
        removed = self._call(
            "/cloud/host/known_key_remove", host_id=self.host.id, key_id=row["id"],
        )["result"]
        self.assertTrue(removed["ok"], removed)
        (row,) = removed["known_keys"]["rows"]
        self.assertFalse(row["active"])
        self.assertEqual(row["removed_by"], "secb-mgr")


class TestSecurityBoardPieces(TransactionCase):
    """The parts of the view that need no central."""

    def setUp(self):
        """A server."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HSecPieces", "ip_address": "203.0.113.84", "user": "root",
            "wildcard_domain": "hsecpieces.example.com",
        })

    def test_a_ban_without_a_lift_after_it_is_banned_now(self):
        """By jail; a lift after the last ban frees the address."""
        now = time.time_ns()
        lines = [
            LogLine(1, ns, line, "fail2ban") for ns, line in (
                _f2b(now, "sshd", "Ban", "192.0.2.1"),
                _f2b(now + 1, "sshd", "Ban", "192.0.2.2"),
                _f2b(now + 2, "sshd", "Unban", "192.0.2.2"),
                _f2b(now + 3, "incubacloud-weblogin", "Ban", "192.0.2.3"),
            )
        ]
        banned, bans = metrics_security.ban_state(lines, "sshd")
        self.assertEqual(banned, {"192.0.2.1"})
        self.assertEqual([ip for _ns, ip in bans], ["192.0.2.1", "192.0.2.2"])

    def test_the_diagnosis_names_a_brute_force_and_open_alerts(self):
        """Over the rule's threshold, and a critical alert open on the server."""
        board = {"end": time.time(), "step": 30, "panels": {"now": {"value": {
            "ssh_failed_10m": 500, "bans_1h": 4, "collector_up": 1, "lines_15m": 30,
        }}}}
        found = diagnose_host_security(board, self.host, now=time.time())
        self.assertEqual([f["code"] for f in found["findings"]], ["ssh_bruteforce"])
        self.env["cloud.alert"].sudo().raise_alert(
            "security_ssh_login_foreign", "Sign-in from 192.0.2.9.",
            level="critical", host=self.host,
        )
        found = diagnose_host_security(board, self.host, now=time.time())
        self.assertEqual(found["tone"], "bad")
        self.assertEqual(found["findings"][0]["code"], "alerts")
        # How many and how grave; the alert's own text, with its address,
        # stays on the Alerts page.
        self.assertNotIn("192.0.2.9", found["findings"][0]["text"])

    def test_a_calm_server_reads_calm(self):
        """Nothing over a threshold and no alert."""
        board = {"end": time.time(), "step": 30, "panels": {"now": {"value": {
            "ssh_failed_10m": 3, "collector_up": 1, "lines_15m": 30,
        }}}}
        found = diagnose_host_security(board, self.host, now=time.time())
        self.assertEqual(found["tone"], "ok")
        self.assertEqual(found["findings"][0]["code"], "calm")

    def test_every_expression_selects_by_the_server_s_id(self):
        """No panel reads beyond its server, and none carries an address."""
        for panel in PANELS["host_security"]:
            for query in panel["queries"]:
                self.assertIn('host_id="%(host_id)d"', query["expr"], panel["key"])
                for label in ("ip", "user", "address"):
                    self.assertNotIn(f"by ({label}", query["expr"])
