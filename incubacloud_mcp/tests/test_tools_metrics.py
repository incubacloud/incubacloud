"""read_metrics: an instance's metrics by the hour, as its Metrics tab may show them.

The tool calls the panel's route, and the route the metrics central, which
is replaced at ``requests.get``: every query of the summary answered as the
central would, from the window and step it asks.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase

from odoo.addons.incubacloud.models.cloud_rate_limit import CloudRateLimit
from odoo.addons.incubacloud.models.metrics_charts import (
    PANELS,
    PEAK_FACTOR,
    summarize_hours,
    summary_step,
)
from odoo.addons.incubacloud_mcp.protocol.limits import MAX_BYTES

from .common import McpHttpMixin, McpUsersMixin, PanelFixtureMixin

_CENTRAL = "odoo.addons.incubacloud.models.cloud_metric_rule.requests.get"
_SERVICE = "container_label_com_docker_compose_service"
#: The point of the window where odoo's CPU jumps.
SPIKE_INDEX = 50
GIB = 1024**3


def _response(payload):
    """Return a response double carrying *payload* as its JSON body."""
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def _series(labels, points):
    """Return one series of a range answer."""
    return {"metric": labels, "values": [[ts, str(value)] for ts, value in points]}


class FakeCentral:
    """Answers each query of the summary from what it selects.

    odoo's CPU is 10 % but for one point (90 %), the database's 5 %; two
    requests a second and no 5xx; p50 0.1 s and p95 0.4 s; odoo uses half
    of its 1 GiB, the database 256 MiB; 12 of 100 connections.
    """

    def __init__(self):
        """Start with no query seen."""
        self.queries = []
        self.window = None
        self.collector_silent = False

    def __call__(self, url, params=None, **_kwargs):
        """Answer ``requests.get`` for one range query of the central."""
        query = params["query"]
        self.queries.append(query)
        start, end, step = params["start"], params["end"], params["step"]
        self.window = (start, end, step)
        stamps = list(range(start, end + 1, step))

        def flat(value):
            """The same value at every step."""
            return [(ts, value) for ts in stamps]

        def role(name, **labels):
            """The labels of one role's series."""
            return {"ic_role": name} | labels

        if "request_duration_seconds_bucket" in query:
            result = [
                _series(role("p50"), flat(0.1)),
                _series(role("p95"), flat(0.4)),
            ]
        elif "traefik_service_requests_total" in query:
            result = [
                _series(role("requests"), flat(2.0)),
                _series(role("5xx"), flat(0.0)),
            ]
        elif "container_cpu_usage_seconds_total" in query:
            odoo = [
                (ts, 90.0 if index == SPIKE_INDEX else 10.0)
                for index, ts in enumerate(stamps)
            ]
            result = [
                _series(role("use", **{_SERVICE: "odoo"}), odoo),
                _series(role("use", **{_SERVICE: "db"}), flat(5.0)),
                _series(role("limit", **{_SERVICE: "odoo"}), flat(200.0)),
            ]
        elif "container_memory_working_set_bytes" in query:
            result = [
                _series(role("use", **{_SERVICE: "odoo"}), flat(GIB / 2)),
                _series(role("use", **{_SERVICE: "db"}), flat(GIB / 4)),
                _series(role("limit", **{_SERVICE: "odoo"}), flat(GIB)),
            ]
        elif "ic_pg_connections" in query:
            result = [] if self.collector_silent else [
                _series(role("connections"), flat(12)),
                _series(role("max_connections"), flat(100)),
            ]
        else:
            raise AssertionError(f"not a query of the summary: {query}")
        return _response(
            {"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    def spike_hour(self):
        """The hour, in ISO 8601, of odoo's CPU spike."""
        start, _end, step = self.window
        moment = start + SPIKE_INDEX * step
        return datetime.fromtimestamp(moment - moment % 3600, timezone.utc).isoformat()


@tagged("post_install", "-at_install")
class TestReadMetrics(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """read_metrics reads the central behind the Metrics tab's gate."""

    def setUp(self):
        """A developer of the project with an API key; observability on."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.env["cloud.settings"]._get_system().write(
            {
                "metrics_enabled": True,
                "metrics_central_url": "http://vm.test:8428",
                "metrics_account": "acct_test",
            }
        )
        self.developer = self._cloud_user("mcp-metrics-dev")
        self.project.member_ids = [(4, self.developer.id)]
        self.key = self._api_key(self.developer)
        self.central = FakeCentral()

    def _read(self, key=None, **args):
        """Call read_metrics on production; return the result and the queries.

        :param str key: the credential, the developer's by default
        """
        arguments = {"instance_id": self.production.id} | args
        with patch(_CENTRAL, side_effect=self.central):
            result = self._call_tool(key or self.key, "read_metrics", arguments)
        return result

    def _ok(self, **args):
        """Call read_metrics, check it went through, return its answer."""
        result = self._read(**args)
        self.assertFalse(result["isError"], result["content"][-1]["text"][:500])
        return self._payload(result)

    def test_the_summary_has_each_hour_with_its_average_and_maximum(self):
        """24 hours by default, five queries about this instance only."""
        answer = self._ok()
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["window_hours"], 24)
        self.assertEqual(len(self.central.queries), 5)
        for query in self.central.queries:
            self.assertIn(f'instance_id="{self.production.id}"', query)
        start, end, step = self.central.window
        self.assertEqual(step, 300)
        self.assertGreaterEqual(end - start, 24 * 3600 - step)
        hours = answer["items"]
        self.assertIn(len(hours), (24, 25))
        row = next(row for row in hours if "peak" not in row)
        self.assertEqual(row["cpu_percent"], {"avg": 15.0, "max": 15.0})
        self.assertEqual(row["memory_bytes"]["max"], GIB * 3 // 4)
        self.assertEqual(row["requests_per_s"], {"avg": 2.0, "max": 2.0})
        self.assertEqual(row["errors_5xx_per_s"], {"avg": 0.0, "max": 0.0})
        self.assertEqual(row["p50_s"]["avg"], 0.1)
        self.assertEqual(row["p95_s"]["max"], 0.4)
        self.assertEqual(row["db_connections"], {"avg": 12, "max": 12})
        self.assertEqual(
            answer["limits"],
            {
                "cpu_percent": {"odoo": 200.0},
                "memory_bytes": {"odoo": GIB},
                "db_connections": 100,
            },
        )
        self.assertEqual(answer["unanswered"], {})
        self.assertNotIn("timing", answer)

    def test_the_peak_is_marked_on_its_hour(self):
        """odoo's spike marks its hour; a flat line is no peak; no 5xx, none."""
        answer = self._ok()
        peak = answer["peaks"]["cpu_percent"]
        self.assertEqual(peak["value"], 95.0)
        self.assertEqual(peak["hour"], self.central.spike_hour())
        self.assertGreaterEqual(peak["times_average"], PEAK_FACTOR)
        self.assertEqual(peak["share_of_limit"], 45.0)
        self.assertEqual(peak["limit_service"], "odoo")
        marked = [row for row in answer["items"] if row.get("peak")]
        self.assertEqual([row["hour"] for row in marked], [peak["hour"]])
        self.assertEqual(marked[0]["peak"], ["cpu_percent"])
        self.assertEqual(marked[0]["cpu_percent"]["max"], 95.0)
        flat = answer["peaks"]["db_connections"]
        self.assertEqual(flat["times_average"], 1.0)
        self.assertEqual(flat["share_of_limit"], 12.0)
        self.assertEqual(answer["peaks"]["memory_bytes"]["value"], GIB * 3 // 4)
        self.assertNotIn("errors_5xx_per_s", answer["peaks"])

    def test_the_points_are_read_at_a_step_that_divides_the_hour(self):
        """A week is read every 30 minutes, a day every 5: each hour has the
        same number of points, so an hour's max is a maximum of them."""
        answer = self._ok(hours=168)
        self.assertEqual(self.central.window[2], 1800)
        self.assertEqual(answer["step"], 1800)
        self.assertEqual(3600 % self.central.window[2], 0)
        answer = self._ok()
        self.assertEqual(self.central.window[2], 300)
        self.assertEqual(answer["step"], 300)

    def test_the_pages_of_a_reading_share_its_window(self):
        """The first answer's end, given back, fixes the next pages' window."""
        first = self._ok(hours=168)
        window = self.central.window
        later = self._ok(hours=168, end=first["end"], offset=first["next_offset"])
        self.assertEqual(self.central.window, window)
        self.assertEqual((later["start"], later["end"]), (first["start"], first["end"]))
        bad = self._read(end="yesterday")
        self.assertTrue(bad["isError"])

    def test_silence_comes_only_when_it_explains_something(self):
        """With readings and a server that reports, none; with a server that
        does not, or an empty central, why."""
        self.assertEqual(self._ok()["silence"]["code"], "agents_missing")
        Instance = type(self.production)
        with patch.object(
            Instance, "_metrics_silence", autospec=True,
            return_value={"code": "window", "since": None},
        ):
            self.assertNotIn("silence", self._ok())

        def empty(url, params=None, **_kwargs):
            """A central that has nothing on the instance."""
            return _response(
                {"status": "success", "data": {"resultType": "matrix", "result": []}}
            )

        with patch(_CENTRAL, side_effect=empty):
            answer = self._payload(
                self._call_tool(
                    self.key, "read_metrics", {"instance_id": self.production.id}
                )
            )
        self.assertEqual(answer["items"], [])
        self.assertIn("code", answer["silence"])

    def test_a_metric_without_readings_says_why(self):
        """The collector silent, or the instance on no server: named, with
        the reason, and the other metrics answered."""
        self.central.collector_silent = True
        answer = self._ok()
        self.assertEqual(list(answer["unanswered"]), ["db_connections"])
        self.assertIn("collector", answer["unanswered"]["db_connections"])
        self.assertIn("cpu_percent", answer["items"][0])
        self.production.sudo().host_id = False
        answer = self._ok()
        self.assertIn("no server", answer["unanswered"]["db_connections"])

    def test_the_connections_are_read_only_while_the_collector_is_fresh(self):
        """Both connection queries keep the collector file's freshness."""
        self._ok()
        queries = [q for q in self.central.queries if "ic_pg_connections" in q]
        self.assertEqual(len(queries), 1)
        self.assertIn("node_textfile_mtime_seconds", queries[0])
        self.assertIn("ic_pg_max_connections", queries[0])
        self.assertEqual(queries[0].count("node_textfile_mtime_seconds"), 2)

    def test_too_many_reads_a_minute_are_refused_in_words(self):
        """The tab's budget: the 21st read is an error and asks nothing."""

        def hit(_self, bucket, **_kwargs):
            """Refuse the tab's bucket only, not the agent's own."""
            return not bucket.startswith("metrics_board_user")

        with patch.object(CloudRateLimit, "hit", autospec=True, side_effect=hit):
            result = self._read()
        self.assertTrue(result["isError"])
        self.assertIn("Too many chart reads", result["content"][-1]["text"])
        self.assertEqual(self.central.queries, [])

    def test_the_window_is_capped_at_a_week_and_comes_in_pages(self):
        """168 hours come in pages under 20 KB that follow each other."""
        result = self._read(hours=168)
        self.assertLessEqual(len(result["content"][-1]["text"].encode()), MAX_BYTES + 512)
        first = self._payload(result)
        self.assertEqual(first["window_hours"], 168)
        self.assertTrue(first["truncated"])
        seen = list(first["items"])
        offset = first["next_offset"]
        while offset is not None:
            page = self._payload(self._read(hours=168, offset=offset))
            self.assertEqual(page["offset"], offset)
            seen.extend(page["items"])
            offset = page["next_offset"]
        self.assertIn(len(seen), (168, 169))
        stamps = [row["hour"] for row in seen]
        self.assertEqual(stamps, sorted(set(stamps)))
        too_long = self._read(hours=169)
        self.assertTrue(too_long["isError"])

    def test_only_who_sees_the_metrics_tab_reads_them(self):
        """A consultant of the project has no such tool, and the route
        refuses them as the tab's board does; an instance out of reach is
        one that does not exist; the central is asked for neither. From
        developer up a role reads every instance, as on the tab."""
        consultant = self._cloud_user("mcp-metrics-cons", group="group_cloud_consultant")
        self.project.member_ids = [(4, consultant.id)]
        consultant_key = self._api_key(consultant)
        self.assertNotIn("read_metrics", self._tool_names(consultant_key))
        self.assertIn("read_metrics", self._tool_names(self.key))
        with patch(_CENTRAL, side_effect=self.central):
            response = self._modern(
                "tools/call",
                {"name": "read_metrics", "arguments": {"instance_id": self.production.id}},
                consultant_key,
            )
            body = response.json()
            self.assertTrue("error" in body or body["result"]["isError"], body)
        result = self._read(instance_id=self.production.id + 10**6)
        self.assertTrue(result["isError"])
        self.assertIn("no instance you can reach", result["content"][-1]["text"])
        with patch(_CENTRAL, side_effect=self.central):
            self.authenticate(consultant.login, consultant.login)
            route = self.url_open(
                "/cloud/instance/metrics_summary",
                data=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "call",
                        "id": 1,
                        "params": {"instance_id": self.production.id},
                    }
                ),
                headers={"Content-Type": "application/json"},
            ).json()
            self.assertIn("error", route)
        self.assertEqual(self.central.queries, [])

    def test_without_observability_or_a_central_it_says_why(self):
        """Off, or without a central: the board's reason, and no query."""
        settings = self.env["cloud.settings"]._get_system()
        settings.metrics_central_url = False
        result = self._read()
        self.assertTrue(result["isError"])
        self.assertEqual(self._payload(result)["reason"], "unconfigured")
        settings.metrics_enabled = False
        result = self._read()
        self.assertTrue(result["isError"])
        self.assertEqual(self._payload(result)["reason"], "disabled")
        self.assertEqual(self.central.queries, [])

    def test_a_central_that_does_not_answer_is_said_so(self):
        """Every query failing is the central down; one failing is named."""
        with patch(_CENTRAL, side_effect=requests.ConnectionError("down")):
            result = self._call_tool(
                self.key, "read_metrics", {"instance_id": self.production.id}
            )
        self.assertTrue(result["isError"])
        self.assertEqual(self._payload(result)["reason"], "central")

        def no_connections(url, params=None, **kwargs):
            """The central, but the connections' query fails."""
            if "ic_pg_connections" in params["query"]:
                raise requests.ConnectionError("down")
            return self.central(url, params=params, **kwargs)

        with patch(_CENTRAL, side_effect=no_connections):
            answer = self._payload(
                self._call_tool(
                    self.key, "read_metrics", {"instance_id": self.production.id}
                )
            )
        self.assertEqual(list(answer["unanswered"]), ["db_connections"])
        self.assertNotIn("db_connections", answer["items"][0])
        self.assertIn("cpu_percent", answer["items"][0])


class TestSummarizeHours(BaseCase):
    """The hourly summary of a board, without a central."""

    def test_it_reuses_the_metrics_tab_panels(self):
        """The summary's panels are the tab's own, plus the connections."""
        tab = {panel["key"]: panel for panel in PANELS["instance"]}
        keys = [panel["key"] for panel in PANELS["instance_summary"]]
        self.assertEqual(keys, ["latency", "requests", "cpu", "memory", "connections"])
        for panel in PANELS["instance_summary"]:
            if panel["key"] in tab:
                self.assertIs(panel, tab[panel["key"]])

    def test_no_reading_gives_no_hour_and_no_peak(self):
        """An empty board: no hours, no peaks, nothing unanswered."""
        board = {
            "panels": {
                panel["key"]: {"kind": "series", "unit": panel["unit"], "series": []}
                for panel in PANELS["instance_summary"]
            }
        }
        self.assertEqual(
            summarize_hours(board),
            {"hours": [], "peaks": {}, "limits": {}, "unanswered": {}},
        )

    def test_the_share_of_the_limit_is_the_largest_of_the_services(self):
        """odoo at its limit with the database at rest is 100 %, not the
        sum of both against the sum of their limits."""
        series = [
            {"name": "odoo", "role": "use", "points": [[3600, 100.0], [3900, 200.0]]},
            {"name": "odoo", "role": "limit", "points": [[3900, 200.0]]},
            {"name": "db", "role": "use", "points": [[3600, 10.0], [3900, 10.0]]},
            {"name": "db", "role": "limit", "points": [[3900, 200.0]]},
        ]
        board = {"panels": {"cpu": {"kind": "series", "series": series}}}
        peak = summarize_hours(board)["peaks"]["cpu_percent"]
        self.assertEqual(peak["value"], 210.0)
        self.assertEqual(peak["share_of_limit"], 100.0)
        self.assertEqual(peak["limit_service"], "odoo")

    def test_a_service_without_a_limit_is_left_out_of_the_share(self):
        """Only a service with a limit has a share."""
        series = [
            {"name": "odoo", "role": "use", "points": [[3600, 50.0], [3900, 150.0]]},
            {"name": "odoo", "role": "limit", "points": [[3600, 200.0]]},
        ]
        board = {"panels": {"cpu": {"kind": "series", "series": series}}}
        peak = summarize_hours(board)["peaks"]["cpu_percent"]
        self.assertEqual(peak["share_of_limit"], 75.0)
        self.assertEqual(peak["times_average"], 1.5)
        series.append({"name": "db", "role": "use", "points": [[3600, 1.0]]})
        peak = summarize_hours(board)["peaks"]["cpu_percent"]
        self.assertEqual(peak["share_of_limit"], 75.0)
        self.assertEqual(peak["limit_service"], "odoo")
        self.assertEqual(peak["value"], 150.0)
        series.pop(1)
        peak = summarize_hours(board)["peaks"]["cpu_percent"]
        self.assertNotIn("share_of_limit", peak)

    def test_the_step_divides_the_hour(self):
        """The board's step, brought down to a divisor of 3600."""
        self.assertEqual(summary_step(30), 30)
        self.assertEqual(summary_step(300), 300)
        self.assertEqual(summary_step(2040), 1800)
        for board_step in range(30, 3600, 30):
            step = summary_step(board_step)
            self.assertEqual(3600 % step, 0)
            self.assertLessEqual(step, board_step)
