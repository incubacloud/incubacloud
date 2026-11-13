"""``/cloud/instance/resource_series``: who may ask, and what comes back.

The board of an instance's Metrics tab: every panel of
``metrics_charts.PANELS["instance"]`` in one answer, with its diagnosis.
Driven over real HTTP, with the metrics central replaced at
``requests.get``.
"""
import json
from unittest.mock import MagicMock, patch

import requests

from odoo.exceptions import UserError
from odoo.tests.common import BaseCase, HttpCase, tagged

from ..models.cloud_rate_limit import CloudRateLimit
from ..models.metrics_charts import (
    CHART_MAX_POINTS,
    CHART_RANGES,
    PANELS,
    chart_window,
    panel_expression,
    shape_series,
)

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"
_SERVICE = "container_label_com_docker_compose_service"


def _response(payload):
    """Return a response double carrying *payload* as its JSON body."""
    resp = MagicMock(spec=requests.Response)
    resp.json.return_value = payload
    return resp


def _central(params):
    """Answer one query of the board as the central would.

    Range queries get two services with three points each, one NaN
    dropped; the instant query of the host panel gets its two roles.

    :param dict params: the query string the route sent.
    """
    if "step" not in params:
        return _response({"status": "success", "data": {
            "resultType": "vector", "result": [
                {"metric": {"ic_role": "cores"}, "value": [0, "4"]},
                {"metric": {"ic_role": "load15"}, "value": [0, "1.5"]},
            ],
        }})
    start, step = params["start"], params["step"]
    values = [[start + i * step, str(i)] for i in range(3)] + [[start, "NaN"]]
    return _response({"status": "success", "data": {
        "resultType": "matrix", "result": [
            {"metric": {_SERVICE: "odoo", "ic_role": "use"}, "values": values},
            {"metric": {_SERVICE: "db", "ic_role": "use"}, "values": values[:1]},
        ],
    }})


@tagged("post_install", "-at_install")
class TestResourceSeries(HttpCase):

    def setUp(self):
        super().setUp()
        self.env["cloud.settings"].sudo()._get_system().write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
            "metrics_account": "acct_test",
            "metrics_remote_write_token": "s3cr3t",
        })
        project = self.env["cloud.project"].create({"name": "Charts"})
        self.host = self.env["cloud.host"].create({
            "name": "HCharts", "ip_address": "203.0.113.61", "user": "root",
            "wildcard_domain": "hcharts.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
        })
        for login, group in (
            ("charts-dev", "incubacloud.group_cloud_developer"),
            ("charts-user", "incubacloud.group_cloud_user"),
        ):
            self.env["res.users"].create({
                "name": login, "login": login, "password": login,
                "groups_id": [(6, 0, [
                    self.env.ref("base.group_user").id, self.env.ref(group).id,
                ])],
            })

    def _call(self, **params):
        """POST the route on the current session and return the JSON body.

        :returns: the whole JSON-RPC answer (``result`` or ``error``).
        """
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": params}
        return self.url_open(
            "/cloud/instance/resource_series",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()

    def test_a_developer_gets_every_panel_in_one_answer(self):
        self.authenticate("charts-dev", "charts-dev")
        with patch(f"{_MODULE}.requests.get",
                   side_effect=lambda url, params, **kw: _central(params)) as get:
            res = self._call(instance_id=self.instance.id, range_key="24h")["result"]
        self.assertTrue(res["ok"], res)
        # One query per panel, whatever the number of lines in it.
        self.assertEqual(get.call_count, len(PANELS["instance"]))
        for call in get.call_args_list:
            query = call.kwargs["params"]["query"]
            self.assertTrue(
                f'instance_id="{self.instance.id}"' in query
                or f'host_id="{self.host.id}"' in query, query,
            )
            self.assertEqual(call.kwargs["auth"], ("acct_test", "s3cr3t"))
        self.assertEqual(res["step"], 300)
        self.assertEqual(set(res["panels"]), {p["key"] for p in PANELS["instance"]})
        cpu = res["panels"]["cpu"]
        self.assertEqual(cpu["unit"], "percent")
        self.assertEqual([s["name"] for s in cpu["series"]], ["db", "odoo"])
        # The NaN sample is dropped; the rest arrive as [ts, value].
        self.assertEqual(len(cpu["series"][1]["points"]), 3)
        self.assertEqual(cpu["series"][1]["points"][2], [res["start"] + 2 * 300, 2.0])
        self.assertEqual(res["panels"]["host"]["value"], {"cores": 4.0, "load15": 1.5})
        self.assertEqual(res["workers"], {"configured": 2})
        self.assertIn("findings", res["diagnosis"])
        self.assertEqual(set(res["timing"]), {"central_ms", "total_ms"})

    def test_an_instance_on_no_host_skips_the_server_panels(self):
        """Pressure, neighbours and the host's numbers need a server."""
        self.instance.host_id = False
        self.authenticate("charts-dev", "charts-dev")
        with patch(f"{_MODULE}.requests.get",
                   side_effect=lambda url, params, **kw: _central(params)) as get:
            res = self._call(instance_id=self.instance.id)["result"]
        self.assertTrue(res["ok"], res)
        server = [p for p in PANELS["instance"] if p["needs"] == "host"]
        self.assertEqual(get.call_count, len(PANELS["instance"]) - len(server))
        self.assertEqual(res["panels"]["neighbours"]["series"], [])

    def test_one_panel_failing_leaves_the_others(self):
        self.authenticate("charts-dev", "charts-dev")

        def answer(url, params, **kw):
            if "node_pressure" in params["query"]:
                raise requests.ConnectionError("down")
            return _central(params)

        with patch(f"{_MODULE}.requests.get", side_effect=answer):
            res = self._call(instance_id=self.instance.id)["result"]
        self.assertTrue(res["ok"], res)
        self.assertIn("error", res["panels"]["pressure"])
        self.assertTrue(res["panels"]["cpu"]["series"])

    def test_a_central_that_fails_says_so(self):
        self.authenticate("charts-dev", "charts-dev")
        with patch(f"{_MODULE}.requests.get",
                   side_effect=requests.ConnectionError("down")) as get:
            res = self._call(instance_id=self.instance.id)["result"]
        self.assertEqual(res["ok"], False)
        self.assertEqual(res["reason"], "central")
        # Each panel is asked once: a retry would double the wait on a
        # central that is already failing.
        self.assertEqual(get.call_count, len(PANELS["instance"]))

    def test_boards_are_limited_per_user(self):
        self.authenticate("charts-dev", "charts-dev")
        uid = self.env["res.users"].search([("login", "=", "charts-dev")]).id
        with patch.object(CloudRateLimit, "hit", autospec=True,
                          return_value=False) as hit, \
                patch(f"{_MODULE}.requests.get") as get:
            res = self._call(instance_id=self.instance.id)["result"]
        self.assertEqual(res, {"ok": False, "error": (
            "Too many chart reads recently. Try again in a minute."
        )})
        self.assertEqual(hit.call_args.args[1], f"metrics_board_user:{uid}")
        self.assertEqual(hit.call_args.kwargs, {"max_per_window": 20})
        get.assert_not_called()

    def test_a_window_out_of_range_is_refused_in_words(self):
        self.authenticate("charts-dev", "charts-dev")
        payload = ('{"jsonrpc": "2.0", "method": "call", "id": 1, "params": '
                   '{"instance_id": %d, "start": 1e400, "end": 1800000000}}'
                   % self.instance.id)
        with patch(f"{_MODULE}.requests.get") as get:
            res = self.url_open(
                "/cloud/instance/resource_series", data=payload,
                headers={"Content-Type": "application/json"},
            ).json()["result"]
        self.assertEqual(res, {"ok": False, "error": "The chart window is not valid."})
        get.assert_not_called()

    def test_observability_off_is_said_without_asking(self):
        self.env["cloud.settings"].sudo()._get_system().metrics_enabled = False
        self.authenticate("charts-dev", "charts-dev")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call(instance_id=self.instance.id)["result"]
        self.assertEqual(res["reason"], "disabled")
        get.assert_not_called()

    def test_a_role_below_developer_is_refused(self):
        self.authenticate("charts-user", "charts-user")
        with patch(f"{_MODULE}.requests.get") as get:
            answer = self._call(instance_id=self.instance.id)
        self.assertIn("error", answer)
        self.assertEqual(answer["error"]["data"]["name"], "odoo.exceptions.AccessError")
        get.assert_not_called()

    def test_an_unknown_instance_is_not_found(self):
        self.authenticate("charts-dev", "charts-dev")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call(instance_id=self.instance.id + 10**6)["result"]
        self.assertEqual(res, {"ok": False, "error": "Instance not found"})
        get.assert_not_called()


class TestChartWindow(BaseCase):

    def test_every_range_stays_within_the_point_budget(self):
        self.assertEqual(set(CHART_RANGES), {"1h", "6h", "24h", "7d"})
        for key, span in CHART_RANGES.items():
            start, end, step = chart_window(key, now=1_800_000_000)
            self.assertLessEqual(span / step, CHART_MAX_POINTS, key)
            self.assertEqual(step % 30, 0, key)
        # A zoom narrower than five minutes is widened, never refused.
        start, end, step = chart_window(start=1_799_999_990, end=1_800_000_000,
                                        now=1_800_000_000)
        self.assertGreaterEqual(end - start, 300)

    def test_a_window_that_overflows_is_not_valid(self):
        with self.assertRaisesRegex(UserError, "not valid"):
            chart_window(start=float("inf"), end=1_800_000_000, now=1_800_000_000)


class TestPanels(BaseCase):

    def _panel(self, key):
        return next(p for p in PANELS["instance"] if p["key"] == key)

    def test_roles_share_one_query_and_peaks_only_on_wide_steps(self):
        memory = self._panel("memory")
        values = {"instance_id": 7, "host_id": 3, "one_off": "x", "window": 120}
        narrow = panel_expression(memory, values, 30)
        wide = panel_expression(memory, values, 2040)
        self.assertEqual(narrow.count('"ic_role"'), 2)
        self.assertIn(" or ", narrow)
        self.assertNotIn("max_over_time", narrow)
        # The working set gets its peaks back; the limit is constant.
        self.assertEqual(wide.count("max_over_time"), 1)
        self.assertIn("[2040s:30s]", wide)

    def test_every_expression_selects_by_id(self):
        values = {"instance_id": 7, "host_id": 3, "one_off": "x", "window": 120}
        for panel in PANELS["instance"]:
            expression = panel_expression(panel, values, 30)
            self.assertTrue(
                'instance_id="7"' in expression or 'host_id="3"' in expression,
                panel["key"],
            )

    def test_a_new_start_time_inside_the_window_is_a_restart(self):
        events = self._panel("events")
        raw = [
            # Started before the window, restarted at 1500 inside it.
            ({_SERVICE: "odoo", "ic_role": "restart"},
             [(1000.0, 500.0), (1300.0, 500.0), (1600.0, 1500.0)]),
            ({_SERVICE: "odoo", "ic_role": "oom"}, [(1300.0, 1.0)]),
        ]
        markers = shape_series(events, raw, 1000, 2000)
        self.assertEqual(markers, [
            {"ts": 1300, "role": "oom", "name": "odoo"},
            {"ts": 1500, "role": "restart", "name": "odoo"},
        ])

    def test_neighbours_keep_the_ten_busiest(self):
        neighbours = self._panel("neighbours")
        raw = [
            ({"instance_id": str(i), "instance": f"i{i}", "ic_role": "use"},
             [(1.0, float(i))])
            for i in range(14)
        ]
        series = shape_series(neighbours, raw, 0, 10)
        self.assertEqual(len(series), 10)
        self.assertNotIn("i0", {s["name"] for s in series})
        self.assertEqual({s["id"] for s in series}, {str(i) for i in range(4, 14)})
