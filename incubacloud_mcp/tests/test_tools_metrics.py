"""read_metrics: an instance's metrics by the hour, as its Metrics tab may show them.

The tool calls the panel's route, and the route the metrics central, which
is replaced at ``requests.get``: every query of the summary answered as the
central would, from the window and step it asks.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase

from odoo.addons.incubacloud.models.metrics_charts import (
    PANELS,
    PEAK_FACTOR,
    summarize_hours,
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
            return {"ic_role": name, **labels}

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
            result = [
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
        arguments = {"instance_id": self.production.id, **args}
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
        self.assertIn("silence", answer)

    def test_the_peak_is_marked_on_its_hour(self):
        """odoo's spike marks its hour; a flat line is no peak; no 5xx, none."""
        answer = self._ok()
        peak = answer["peaks"]["cpu_percent"]
        self.assertEqual(peak["value"], 95.0)
        self.assertEqual(peak["hour"], self.central.spike_hour())
        self.assertGreaterEqual(peak["times_average"], PEAK_FACTOR)
        self.assertNotIn("share_of_limit", peak)
        marked = [row for row in answer["items"] if row.get("peak")]
        self.assertEqual([row["hour"] for row in marked], [peak["hour"]])
        self.assertEqual(marked[0]["peak"], ["cpu_percent"])
        self.assertEqual(marked[0]["cpu_percent"]["max"], 95.0)
        flat = answer["peaks"]["db_connections"]
        self.assertEqual(flat["times_average"], 1.0)
        self.assertEqual(flat["share_of_limit"], 12.0)
        self.assertEqual(answer["peaks"]["memory_bytes"]["value"], GIB * 3 // 4)
        self.assertNotIn("errors_5xx_per_s", answer["peaks"])

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
        """A consultant of the project has no such tool; a developer outside
        the project is told the instance is not there; the central is not
        asked for either."""
        consultant = self._cloud_user("mcp-metrics-cons", group="group_cloud_consultant")
        self.project.member_ids = [(4, consultant.id)]
        consultant_key = self._api_key(consultant)
        self.assertNotIn("read_metrics", self._tool_names(consultant_key))
        self.assertIn("read_metrics", self._tool_names(self.key))
        outsider_key = self._api_key(self._cloud_user("mcp-metrics-out"))
        for key in (consultant_key, outsider_key):
            response = self._modern(
                "tools/call",
                {"name": "read_metrics", "arguments": {"instance_id": self.production.id}},
                key,
            )
            body = response.json()
            refused = "error" in body or body["result"]["isError"]
            self.assertTrue(refused, body)
        result = self._read(key=outsider_key)
        self.assertIn("no instance you can reach", result["content"][-1]["text"])
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

    def test_a_limit_short_of_a_service_gives_no_share(self):
        """A share of the limit only when every service has one."""
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
        self.assertNotIn("share_of_limit", peak)
        self.assertEqual(peak["value"], 150.0)
