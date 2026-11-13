"""The Metrics tab's diagnosis and its readings on demand.

The diagnosis turns a board into sentences (``metrics_diagnosis``); the
two readings, "Queries now" and "Slow routes", ask the instance's host
over SSH when somebody presses their button. Here the host is replaced
at the controller's ``_read_with_script``, and what the scripts print is
taken from their documented shape; ``_read_with_script`` itself is run
against a local bash, with only the SSH connection replaced.
"""
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
from unittest.mock import MagicMock, patch

import asyncssh
from asyncssh.misc import _ACMWrapper

from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..controllers._data_load import _routes_ops
from ..controllers._data_load._routes_ops import OpsMixin
from ..models.cloud_rate_limit import CloudRateLimit
from ..models.cloud_instance_slow_routes import parse_slow_routes, route_key
from ..models.metrics_diagnosis import diagnose_instance

_SERVICE = "container_label_com_docker_compose_service"

#: What ``read_odoo_log.sh`` prints for a quarter of an hour.
_LOG = "\n".join([
    "R\t2026-11-10 09:40:01\tPOST /web/dataset/call_kw/stock.quant/action_apply"
    "#stock.quant.action_apply HTTP/1.1\t200\t152\t3.400\t0.500",
    "R\t2026-11-10 09:41:01\tPOST /web/dataset/call_kw/stock.quant/action_apply"
    "#stock.quant.action_apply HTTP/1.1\t200\t140\t2.600\t0.500",
    "R\t2026-11-10 09:42:00\tGET /web/image/res.partner/5/avatar_128 HTTP/1.1"
    "\t304\t2\t0.002\t0.010",
    "R\t2026-11-10 09:42:01\tGET /web/image/res.partner/9/avatar_128?unique=1"
    " HTTP/1.1\t200\t2\t0.003\t0.011",
    "R\t2026-11-10 09:43:00\tGET /web/login HTTP/1.1\t200\t-\t-\t-",
    "C\t2026-11-10 09:40:02\t13\tJob 'Inventory: value' (7) starting",
    "C\t2026-11-10 09:45:12\t13\tJob 'Inventory: value' (7) fully done"
    " (#loop 1; done 3; remaining 0; duration 310.00s)",
    "C\t2026-11-10 09:46:00\t14\tStarting job `Mail: send`.",
    "C\t2026-11-10 09:46:30\t14\tJob `Mail: send` done.",
    "C\t2026-11-10 09:47:00\t15\tJob 'Big import' (9) starting",
    "M\t2026-11-10 09:46:00",
    "M\t2026-11-10 09:48:00",
    # With workers: the master kills the worker and logs no trace.
    "K\t2026-11-10 09:47:00\tWorkerHTTP (56) timeout after 1200s",
    # Without workers: the stack Odoo dumps, header and innermost frames.
    "K\t2026-11-10 09:50:59\tDumping stacktrace of limit exceeding threads"
    " before reloading",
    "+\t# Thread: <Thread(odoo.service.http.request.140245, started 140245)>"
    " (db:prod) (uid:2) (url:http://prod.example.com/web/dataset/call_kw/"
    "stock.quant/action_apply) (qc:5210 qt:12.004 pt:118.310)",
    "+\tFile: \"/opt/odoo/custom/src/private/stock_x/models/stock_quant.py\","
    " line 91, in action_apply",
    "+\t  quant._recompute_value()",
])


class TestSlowRoutesParsing(TransactionCase):

    def test_a_route_is_counted_without_its_ids_nor_its_query_string(self):
        self.assertEqual(
            route_key("GET /web/image/res.partner/5/avatar_128?unique=1 HTTP/1.1"),
            "GET /web/image/res.partner/:id/avatar_128",
        )
        self.assertEqual(
            route_key("POST /web/dataset/call_kw/sale.order/read#sale.order.read HTTP/1.1"),
            "POST /web/dataset/call_kw/sale.order/read#sale.order.read",
        )

    def test_the_slowest_route_comes_first_with_its_sql_share(self):
        out = parse_slow_routes(_LOG, 15)
        self.assertEqual(out["requests"], 5)
        self.assertEqual(out["untimed"], 1)
        top = out["routes"][0]
        self.assertEqual(
            top["route"],
            "POST /web/dataset/call_kw/stock.quant/action_apply#stock.quant.action_apply",
        )
        self.assertEqual((top["count"], top["total"], top["mean"]), (2, 7.0, 3.5))
        self.assertEqual((top["sql_share"], top["python_share"]), (86, 14))
        self.assertEqual(top["queries"], 146)
        images = out["routes"][1]
        self.assertEqual(images["count"], 2)

    def test_scheduled_actions_with_their_duration_in_any_series(self):
        crons = {c["name"]: c for c in parse_slow_routes(_LOG, 15)["crons"]}
        self.assertEqual(crons["Inventory: value"]["max"], 310.0)
        # An older series logs no duration: the start and the end say it.
        self.assertEqual(crons["Mail: send"]["max"], 30.0)
        self.assertEqual(crons["Big import"]["running_since"], "2026-11-10T09:47:00Z")
        # Still running comes first: it is the likeliest culprit.
        self.assertEqual(parse_slow_routes(_LOG, 15)["crons"][0]["name"], "Big import")

    def test_kills_carry_the_stack_odoo_logged_and_recycles_a_rate(self):
        out = parse_slow_routes(_LOG, 15)
        self.assertEqual(out["kills"][0], {
            "at": "2026-11-10T09:47:00Z",
            "message": "WorkerHTTP (56) timeout after 1200s",
            "trace": [],
        })
        dumped = out["kills"][1]
        self.assertEqual(dumped["at"], "2026-11-10T09:50:59Z")
        self.assertEqual(len(dumped["trace"]), 3)
        self.assertIn("(url:http://prod.example.com/web/dataset/call_kw/"
                      "stock.quant/action_apply)", dumped["trace"][0])
        self.assertEqual(dumped["trace"][-1], "  quant._recompute_value()")
        # Two threads over the limit at once: ten lines of each.
        two = "\n".join(["K\t2026-11-10 09:50:59\tDumping stacktrace"]
                        + [f"+\tline {i}" for i in range(20)])
        self.assertEqual(len(parse_slow_routes(two, 15)["kills"][0]["trace"]), 20)
        self.assertEqual(out["recycles"], {"count": 2, "per_hour": 8.0})

    def test_an_empty_log_is_an_empty_reading(self):
        out = parse_slow_routes("", 60)
        self.assertEqual((out["requests"], out["routes"], out["crons"]), (0, [], []))


#: When the boards of the diagnosis tests end, unix seconds.
_END = 1_800_000_000


def _board(**panels):
    """A board ending at ``_END`` with the given panels, for the diagnosis."""
    end = _END
    base = {
        "start": end - 3600, "end": end, "step": 30,
        "panels": {
            "host": {"kind": "stat", "value": {"cores": 4, "load15": 1}},
            "events": {"kind": "markers", "markers": []},
        },
    }
    for key, series in panels.items():
        base["panels"][key] = series
    return base, end


def _line(name, role, value, end):
    """One flat series over the last 15 minutes of a board."""
    return {"name": name, "role": role, "id": "",
            "points": [[end - 600, value], [end, value]]}


class TestDiagnosis(TransactionCase):

    def setUp(self):
        super().setUp()
        project = self.env["cloud.project"].create({"name": "Diag"})
        self.instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": project.id, "environment": "production",
        })

    def _codes(self, board, now):
        return [f["code"] for f in diagnose_instance(board, self.instance, 2, now)["findings"]]

    def test_a_quiet_instance_says_nothing_is_saturated(self):
        board, end = _board(cpu={"series": [_line("odoo", "use", 5, _END)]})
        result = diagnose_instance(board, self.instance, 2, end)
        self.assertEqual(result["tone"], "ok")
        self.assertEqual([f["code"] for f in result["findings"]], ["nothing_saturated"])
        self.assertEqual(result["facts"]["occupancy"], 2.5)

    def test_a_throttled_odoo_names_its_quota(self):
        board, end = _board(
            throttled={"series": [_line("odoo", "use", 40, _END)]},
            cpu={"series": [_line("odoo", "limit", 200, _END)]},
        )
        result = diagnose_instance(board, self.instance, 2, end)
        self.assertEqual(result["tone"], "warn")
        finding = result["findings"][0]
        self.assertEqual(finding["code"], "cpu_throttled")
        self.assertIn("(2 CPUs)", finding["text"])
        self.assertIn("40%", finding["text"])

    def test_a_busy_neighbour_on_a_saturated_server_is_named(self):
        board, end = _board(neighbours={"series": [
            {**_line("acme-prod", "use", 300, _END), "id": "99"},
            {**_line("prod", "use", 10, _END), "id": str(self.instance.id)},
        ]})
        board["panels"]["host"]["value"] = {"cores": 4, "load15": 12}
        result = diagnose_instance(board, self.instance, 2, end)
        self.assertEqual(result["findings"][0]["code"], "host_neighbour")
        self.assertIn("acme-prod", result["findings"][0]["text"])

    def test_an_oom_kill_or_memory_pressure_is_bad(self):
        board, end = _board()
        board["panels"]["events"]["markers"] = [{"ts": end - 60, "role": "oom", "name": "odoo"}]
        self.assertEqual(diagnose_instance(board, self.instance, 2, end)["tone"], "bad")
        board, end = _board(pressure={"series": [_line("", "memory", 15, _END)]})
        self.assertIn("memory_short", self._codes(board, end))

    def test_slow_requests_with_idle_cpu_point_outside(self):
        board, end = _board(
            latency={"series": [_line("", "p95", 4.8, _END)]},
            cpu={"series": [_line("odoo", "use", 5, _END), _line("db", "use", 2, _END)]},
        )
        self.assertIn("slow_idle", self._codes(board, end))

    def test_a_restart_is_told_with_how_long_ago(self):
        board, end = _board()
        board["panels"]["events"]["markers"] = [
            {"ts": end - 720, "role": "restart", "name": "odoo"},
        ]
        result = diagnose_instance(board, self.instance, 2, end)
        self.assertEqual([f["code"] for f in result["findings"]],
                         ["restarted", "nothing_saturated"])
        self.assertIn("12 minutes", result["findings"][0]["text"])

    def test_only_the_last_15_minutes_restarts_and_kills_count(self):
        """The card reads the last quarter of an hour, its moments too."""
        board, end = _board()
        board["panels"]["events"]["markers"] = [
            {"ts": end - 1800, "role": "oom", "name": "odoo"},
            {"ts": end - 3000, "role": "restart", "name": "odoo"},
        ]
        result = diagnose_instance(board, self.instance, 2, end)
        self.assertEqual(result["tone"], "ok")
        self.assertEqual([f["code"] for f in result["findings"]], ["nothing_saturated"])
        self.assertEqual((result["facts"]["oom"], result["facts"]["last_restart"]),
                         (False, None))

    def test_a_window_in_the_past_gets_no_diagnosis(self):
        board, end = _board(throttled={"series": [_line("odoo", "use", 90, _END)]})
        result = diagnose_instance(board, self.instance, 2, end + 86400)
        self.assertEqual((result["current"], result["findings"]), (False, []))


@tagged("post_install", "-at_install")
class TestReadingRoutes(HttpCase):

    def setUp(self):
        super().setUp()
        project = self.env["cloud.project"].create({"name": "Readings"})
        self.host = self.env["cloud.host"].create({
            "name": "HRead", "ip_address": "203.0.113.62", "user": "root",
            "wildcard_domain": "hread.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": project.id,
            "environment": "staging", "host_id": self.host.id,
            "postgres_dbname": "prod", "state": "deployed",
        })
        self.project = project
        for login, group in (
            ("read-dev", "incubacloud.group_cloud_developer"),
            ("read-user", "incubacloud.group_cloud_user"),
        ):
            self.env["res.users"].create({
                "name": login, "login": login, "password": login,
                "groups_id": [(6, 0, [
                    self.env.ref("base.group_user").id, self.env.ref(group).id,
                ])],
            })

    def _call(self, route, **params):
        """POST *route* on the current session and return the JSON body."""
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": params}
        return self.url_open(
            route, data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()

    def test_queries_now_returns_the_sessions_the_script_read(self):
        reading = {
            "summary": {"connections": 3, "max_connections": 100, "active": 1,
                        "waiting_lock": 1, "idle_in_transaction": 1,
                        "longest_active": 372, "size_bytes": 7},
            "sessions": [{"pid": 812, "state": "active", "wait_event_type": "Lock",
                          "blocked_by": [791], "blocks": [], "query": "UPDATE x",
                          "password": "never passed on"}],
            "tables": [{"table": "stock_quant", "live": 10, "dead": 4,
                        "last_vacuum": None}],
        }
        self.authenticate("read-dev", "read-dev")
        with patch.object(OpsMixin, "_read_with_script", autospec=True,
                          return_value=("warning first\n" + json.dumps(reading), "", 0)) as read:
            res = self._call("/cloud/instance/db_activity",
                             instance_id=self.instance.id)["result"]
        self.assertTrue(res["ok"], res)
        _self, host, name, args = read.call_args.args
        self.assertEqual((host, name), (self.host, "read_pg_activity.sh"))
        self.assertEqual(args, [self.instance.get_remote_dir(), "prod"])
        self.assertEqual(res["summary"]["waiting_lock"], 1)
        self.assertEqual(res["sessions"][0]["blocked_by"], [791])
        self.assertNotIn("password", res["sessions"][0])
        self.assertIn("read_at", res)

    def test_a_failed_reading_says_so(self):
        self.authenticate("read-dev", "read-dev")
        with patch.object(OpsMixin, "_read_with_script", autospec=True,
                          return_value=("", "Error: service odoo is not running", 1)):
            res = self._call("/cloud/instance/db_activity",
                             instance_id=self.instance.id)["result"]
        self.assertFalse(res["ok"])
        self.assertIn("could not be read", res["error"])

    def test_slow_routes_reads_the_window_asked_for(self):
        self.authenticate("read-dev", "read-dev")
        with patch.object(OpsMixin, "_read_with_script", autospec=True,
                          return_value=(_LOG, "", 0)) as read:
            res = self._call("/cloud/instance/slow_routes",
                             instance_id=self.instance.id, minutes=60)["result"]
        self.assertTrue(res["ok"], res)
        _self, _host, name, args = read.call_args.args
        self.assertEqual(name, "read_odoo_log.sh")
        self.assertEqual(args[0], self.instance.get_remote_dir())
        self.assertRegex(args[1], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(args[2], 60)
        self.assertEqual(res["minutes"], 60)
        self.assertEqual(res["routes"][0]["count"], 2)

    def test_an_unknown_window_is_refused_without_reading(self):
        self.authenticate("read-dev", "read-dev")
        with patch.object(OpsMixin, "_read_with_script", autospec=True) as read:
            res = self._call("/cloud/instance/slow_routes",
                             instance_id=self.instance.id, minutes=5000)["result"]
        self.assertFalse(res["ok"])
        read.assert_not_called()

    def test_an_instance_not_deployed_has_nothing_to_read(self):
        draft = self.env["cloud.instance"].create({
            "name": "draft", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
        })
        self.authenticate("read-dev", "read-dev")
        with patch.object(OpsMixin, "_read_with_script", autospec=True) as read:
            res = self._call("/cloud/instance/db_activity",
                             instance_id=draft.id)["result"]
        self.assertFalse(res["ok"])
        self.assertIn("not deployed", res["error"])
        read.assert_not_called()

    def test_an_instance_on_no_host_says_so(self):
        """Not the proxy log's words: these readings read the database and the log."""
        self.instance.host_id = False
        self.authenticate("read-dev", "read-dev")
        for route in ("/cloud/instance/db_activity", "/cloud/instance/slow_routes"):
            with patch.object(OpsMixin, "_read_with_script", autospec=True) as read:
                res = self._call(route, instance_id=self.instance.id)["result"]
            self.assertEqual(res, {"ok": False, "error": (
                "This instance is not placed on a host yet, so there is "
                "nothing to read on it."
            )}, route)
            read.assert_not_called()

    def test_readings_are_limited_per_user_as_the_logs_are(self):
        self.authenticate("read-dev", "read-dev")
        uid = self.env["res.users"].search([("login", "=", "read-dev")]).id
        for route, bucket, words in (
            ("/cloud/instance/db_activity", f"logs_db_activity_user:{uid}",
             "Too many database reads recently"),
            ("/cloud/instance/slow_routes", f"logs_slow_routes_user:{uid}",
             "Too many log reads recently"),
        ):
            with patch.object(CloudRateLimit, "hit", autospec=True,
                              return_value=False) as hit, \
                    patch.object(OpsMixin, "_read_with_script", autospec=True) as read:
                res = self._call(route, instance_id=self.instance.id)["result"]
            self.assertFalse(res["ok"], route)
            self.assertIn(words, res["error"])
            self.assertEqual(hit.call_args.args[1], bucket)
            self.assertEqual(hit.call_args.kwargs, {"cap_key": "rate_limit_logs_per_min"})
            read.assert_not_called()

    def test_a_role_below_developer_reads_nothing(self):
        self.authenticate("read-user", "read-user")
        for route in ("/cloud/instance/db_activity", "/cloud/instance/slow_routes",
                      "/cloud/instance/access_log"):
            with patch.object(OpsMixin, "_read_with_script", autospec=True) as read, \
                    patch.object(OpsMixin, "_ssh_run", autospec=True) as ssh:
                answer = self._call(route, instance_id=self.instance.id)
            self.assertEqual(answer["error"]["data"]["name"],
                             "odoo.exceptions.AccessError", route)
            read.assert_not_called()
            ssh.assert_not_called()

    def test_an_unknown_instance_is_not_found(self):
        self.authenticate("read-dev", "read-dev")
        for route in ("/cloud/instance/db_activity", "/cloud/instance/slow_routes",
                      "/cloud/instance/access_log"):
            res = self._call(route, instance_id=self.instance.id + 10**6)["result"]
            self.assertEqual(res, {"ok": False, "error": "Instance not found"}, route)


#: A script standing in for the readings': it prints its arguments, what
#: it finds on its standard input and where its own text was written.
_PROBE = """printf 'arg=%s\\n' "$@"
if read -r line; then echo "stdin=$line"; else echo "stdin=closed"; fi
echo "self=$0"
stat -c 'mode=%a' "$0"
"""


class TestReadWithScript(TransactionCase):
    """``OpsMixin._read_with_script``: the command it sends, run by bash here."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HScript", "ip_address": "203.0.113.63", "user": "root",
            "wildcard_domain": "hscript.example.com",
        })
        scripts = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scripts)
        (scripts / "probe.sh").write_text(_PROBE)
        patcher = patch.object(_routes_ops, "_SCRIPTS_DIR", scripts)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(
            type(self.host), "ssh_connect_kwargs", autospec=True,
            return_value={"host": "203.0.113.63", "connect_timeout": 30,
                          "login_timeout": 30},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connection(self, conn, login_seconds=0):
        """What ``asyncssh.connect()`` hands back, the login taking *login_seconds*."""
        async def enter(*_args):
            await asyncio.sleep(login_seconds)
            return conn

        connection = MagicMock(spec=_ACMWrapper)
        connection.__aenter__.side_effect = enter
        connection.__aexit__.return_value = False
        return connection

    def _bash_conn(self):
        """A connection whose ``run`` runs the command in a local bash."""
        def run(command, input=None, check=False):  # noqa: A002 — asyncssh's name
            done = subprocess.run(
                ["bash", "-c", command], input=input, capture_output=True,
                text=True, timeout=10, check=False,
            )
            return MagicMock(spec=asyncssh.SSHCompletedProcess, stdout=done.stdout,
                             stderr=done.stderr, exit_status=done.returncode)

        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.run.side_effect = run
        return conn

    def test_arguments_are_quoted_and_the_script_runs_from_a_private_file(self):
        conn = self._bash_conn()
        with patch.object(asyncssh, "connect", return_value=self._connection(conn)) as connect:
            out, err, status = OpsMixin()._read_with_script(
                self.host, "probe.sh",
                ["~/instances/a b", "it's; touch /tmp/ic-pwned", 15],
            )
        self.assertEqual((status, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[:3], [
            "arg=" + os.path.expanduser("~/instances/a b"),
            "arg=it's; touch /tmp/ic-pwned",
            "arg=15",
        ])
        self.assertFalse(pathlib.Path("/tmp/ic-pwned").exists())
        # The script's own text went to its file, not to its standard input.
        self.assertEqual(lines[3], "stdin=closed")
        command = conn.run.call_args.args[0]
        self.assertIn('f="$(mktemp)"', command)
        self.assertTrue(command.endswith("</dev/null"), command)
        self.assertEqual(conn.run.call_args.kwargs["input"], _PROBE)
        mine = lines[4].removeprefix("self=")
        self.assertEqual(lines[5], "mode=600")
        self.assertFalse(pathlib.Path(mine).exists(), "the temporary file is removed")
        # The connection and the login share the reading's deadline.
        kwargs = connect.call_args.kwargs
        self.assertEqual((kwargs["connect_timeout"], kwargs["login_timeout"]), (20, 20))

    def test_a_slow_login_counts_against_the_deadline(self):
        conn = self._bash_conn()
        with patch.object(asyncssh, "connect",
                          return_value=self._connection(conn, login_seconds=2)):
            began = time.monotonic()
            # asyncio's own: the built-in TimeoutError only from Python 3.11.
            with self.assertRaises(asyncio.TimeoutError):
                OpsMixin()._read_with_script(self.host, "probe.sh", [], timeout=0.3)
        self.assertLess(time.monotonic() - began, 1.5)
        conn.run.assert_not_called()
