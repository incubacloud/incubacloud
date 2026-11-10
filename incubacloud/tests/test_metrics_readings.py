"""The Metrics tab's diagnosis and its readings on demand.

The diagnosis turns a board into sentences (``metrics_diagnosis``); the
two readings, "Queries now" and "Slow routes", ask the instance's host
over SSH when somebody presses their button. Here the host is replaced
at the controller's ``_read_with_script``, and what the scripts print is
taken from their documented shape.
"""
import json
from unittest.mock import patch

from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..controllers._data_load._routes_ops import OpsMixin
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
    "K\t2026-11-10 09:47:00\tWorkerHTTP (56) timeout after 1200s",
    "+\tTraceback (most recent call last):",
    "+\t  File \"x.py\", line 1",
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

    def test_kills_carry_the_start_of_their_trace_and_recycles_a_rate(self):
        out = parse_slow_routes(_LOG, 15)
        self.assertEqual(out["kills"], [{
            "at": "2026-11-10T09:47:00Z",
            "message": "WorkerHTTP (56) timeout after 1200s",
            "trace": ["Traceback (most recent call last):", '  File "x.py", line 1'],
        }])
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
                "group_ids": [(6, 0, [
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

    def test_a_role_below_developer_reads_nothing(self):
        self.authenticate("read-user", "read-user")
        for route in ("/cloud/instance/db_activity", "/cloud/instance/slow_routes",
                      "/cloud/instance/access_log"):
            with patch.object(OpsMixin, "_read_with_script", autospec=True) as read:
                answer = self._call(route, instance_id=self.instance.id)
            self.assertEqual(answer["error"]["data"]["name"],
                             "odoo.exceptions.AccessError", route)
            read.assert_not_called()

    def test_an_unknown_instance_is_not_found(self):
        self.authenticate("read-dev", "read-dev")
        for route in ("/cloud/instance/db_activity", "/cloud/instance/slow_routes",
                      "/cloud/instance/access_log"):
            res = self._call(route, instance_id=self.instance.id + 10**6)["result"]
            self.assertEqual(res, {"ok": False, "error": "Instance not found"}, route)
