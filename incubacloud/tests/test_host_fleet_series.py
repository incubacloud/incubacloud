"""``/cloud/host/resource_series`` and ``/cloud/fleet/resource_series``.

A server's board (its Metrics tab and Monitoring's Hosts view) and the
fleet's (Monitoring's Fleet view): who may ask, which credential reads,
and what comes back. Driven over real HTTP, with the metrics central
replaced at ``requests.get``.
"""
import json
import re
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import BaseCase, HttpCase, TransactionCase, tagged

from ..models.metrics_charts import (
    _MAX_SERIES,
    _MAX_STATE_ROWS,
    PANELS,
    panel_expression,
    shape_series,
    shape_state,
    shape_table,
)
from ..models.metrics_diagnosis import diagnose_host

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"
_GIB = 1024 ** 3
_ROLES = re.compile(r'"ic_role", "([^"]+)"')


def _response(payload):
    """Return a response double carrying *payload* as its JSON body."""
    resp = MagicMock(spec=requests.Response)
    resp.json.return_value = payload
    return resp


def _refusal(status):
    """Return a response double the central sends to a credential it refuses.

    :param int status: 401 or 403; 429, the central busy.
    """
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.raise_for_status.side_effect = requests.HTTPError(
        f"{status} Client Error", response=resp,
    )
    return resp


def _vector(rows):
    """Return an instant answer of ``[(labels, value)]``."""
    return _response({"status": "success", "data": {
        "resultType": "vector",
        "result": [{"metric": m, "value": [0, str(v)]} for m, v in rows],
    }})


def _matrix(params, series):
    """Return a range answer covering the whole window asked.

    :param dict params: the query string the route sent.
    :param list series: ``[(labels, value)]``, each drawn flat.
    """
    start, end, step = params["start"], params["end"], params["step"]
    stamps = range(start, end + 1, step)
    return _response({"status": "success", "data": {
        "resultType": "matrix",
        "result": [
            {"metric": m, "values": [[ts, str(v)] for ts in stamps]}
            for m, v in series
        ],
    }})


def _host_central(instance_id):
    """Answer the server board's queries as a central with one busy instance.

    The server's memory reads 95 % and the instance uses most of it.

    :param int instance_id: the instance the series are labelled with.
    """
    inst = {"instance_id": str(instance_id), "instance": "prod"}

    def answer(url, params, **kw):
        roles = _ROLES.findall(params["query"])
        if "step" not in params:
            if "cores" in roles:
                return _vector([
                    ({"ic_role": "cores"}, 4),
                    ({"ic_role": "memory_total"}, 8 * _GIB),
                    ({"ic_role": "root_size"}, 80 * _GIB),
                    ({"ic_role": "ingest_age"}, 12),
                ])
            return _vector([
                (inst | {"ic_role": "cpu"}, 150),
                (inst | {"ic_role": "memory"}, 6 * _GIB),
                ({"instance_id": str(instance_id), "ic_role": "disk"}, 2 * _GIB),
                ({"instance_id": "999999", "instance": "elsewhere",
                  "ic_role": "cpu"}, 5),
            ])
        value = {"used": 95}.get(roles[0], 1)
        return _matrix(params, [(inst | {"ic_role": role}, value) for role in roles])

    return answer


@tagged("post_install", "-at_install")
class TestHostSeries(HttpCase):

    def setUp(self):
        super().setUp()
        self.env["cloud.settings"].sudo()._get_system().write({
            "metrics_enabled": True,
            "metrics_central_url": "https://central.test/r",
            "metrics_account": "acct_test",
            "metrics_remote_write_token": "s3cr3t",
        })
        self.project = self.env["cloud.project"].create({"name": "HostCharts"})
        self.host = self.env["cloud.host"].create({
            "name": "HHostCharts", "ip_address": "203.0.113.71", "user": "root",
            "wildcard_domain": "hhostcharts.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
        })
        for login, group in (
            ("hcharts-dev", "incubacloud.group_cloud_developer"),
            ("hcharts-user", "incubacloud.group_cloud_user"),
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

    def test_a_developer_gets_every_server_panel_in_one_answer(self):
        self.authenticate("hcharts-dev", "hcharts-dev")
        with patch(f"{_MODULE}.requests.get",
                   side_effect=_host_central(self.instance.id)) as get:
            res = self._call("/cloud/host/resource_series",
                             host_id=self.host.id, range_key="1h")["result"]
        self.assertTrue(res["ok"], res)
        # One query per panel, each about this server, with the account.
        self.assertEqual(get.call_count, len(PANELS["host"]))
        for call in get.call_args_list:
            self.assertIn(f'host_id="{self.host.id}"', call.kwargs["params"]["query"])
            self.assertEqual(call.kwargs["auth"], ("acct_test", "s3cr3t"))
            self.assertTrue(call.args[0].startswith("https://central.test/r/api/v1/"))
        self.assertEqual(set(res["panels"]), {p["key"] for p in PANELS["host"]})
        self.assertEqual(res["panels"]["host"]["value"]["cores"], 4)
        # The table names the instance it can open, busiest first; an
        # instance this panel does not know keeps the agents' name.
        rows = res["panels"]["consumers"]["rows"]
        self.assertEqual([r["name"] for r in rows], ["prod", "elsewhere"])
        self.assertEqual(rows[0]["project_id"], self.project.id)
        self.assertFalse(rows[1]["project_id"])
        # Memory at 95 % names the instance that uses most of it.
        codes = {f["code"]: f for f in res["diagnosis"]["findings"]}
        self.assertIn("memory_high", codes)
        self.assertIn("prod", codes["memory_high"]["text"])
        self.assertEqual(res["silence"]["code"], "agents_missing")
        self.assertIn("total_ms", res["timing"])

    def test_a_central_that_refuses_the_credential_says_so(self):
        self.authenticate("hcharts-dev", "hcharts-dev")
        with patch(f"{_MODULE}.requests.get", return_value=_refusal(401)):
            res = self._call("/cloud/host/resource_series",
                             host_id=self.host.id)["result"]
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "credential")

    def test_observability_off_is_said_without_asking(self):
        self.env["cloud.settings"].sudo()._get_system().metrics_enabled = False
        self.authenticate("hcharts-dev", "hcharts-dev")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/resource_series",
                             host_id=self.host.id)["result"]
        self.assertEqual(res["reason"], "disabled")
        get.assert_not_called()

    def test_a_role_below_developer_is_refused(self):
        self.authenticate("hcharts-user", "hcharts-user")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/resource_series", host_id=self.host.id)
        self.assertIn("error", res)
        get.assert_not_called()

    def test_an_unknown_host_is_not_found(self):
        self.authenticate("hcharts-dev", "hcharts-dev")
        with patch(f"{_MODULE}.requests.get") as get:
            res = self._call("/cloud/host/resource_series",
                             host_id=self.host.id + 10000)["result"]
        self.assertEqual(res, {"ok": False, "error": "Host not found"})
        get.assert_not_called()


def _fleet_central(refuse_operator=False):
    """Answer the fleet board's queries; the operator's route may refuse.

    Two hosts: one of this panel's account, one of another account.

    :param bool refuse_operator: answer 403 on ``/admin-r``.
    """
    hosts = [
        {"ic_account": "acct_test", "host_id": "3", "host": "mine"},
        {"ic_account": "acct_other", "host_id": "3", "host": "theirs"},
    ]

    def answer(url, params, **kw):
        if refuse_operator and "/admin-r/" in url:
            return _refusal(403)
        roles = _ROLES.findall(params["query"])
        if "step" not in params:
            return _vector([({"ic_role": role}, 2) for role in roles])
        return _matrix(params, [(h | {"ic_role": roles[0]}, 1) for h in hosts])

    return answer


@tagged("post_install", "-at_install")
class TestFleetSeries(HttpCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "https://central.test/r",
            "metrics_account": "acct_test",
            "metrics_remote_write_token": "s3cr3t",
            "metrics_operator_token": "op3r",
        })
        self.env["res.users"].create({
            "name": "fcharts-dev", "login": "fcharts-dev", "password": "fcharts-dev",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_developer").id,
            ])],
        })
        self.authenticate("fcharts-dev", "fcharts-dev")

    def _call(self, **params):
        """POST the fleet route and return its result."""
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": params}
        return self.url_open(
            "/cloud/fleet/resource_series", data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()["result"]

    def test_the_central_s_owner_reads_every_account(self):
        with patch(f"{_MODULE}.requests.get", side_effect=_fleet_central()) as get:
            res = self._call(range_key="6h")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["scope"], "operator")
        self.assertEqual(get.call_count, len(PANELS["fleet"]))
        for call in get.call_args_list:
            self.assertTrue(call.args[0].startswith("https://central.test/admin-r/api/v1/"))
            self.assertEqual(call.kwargs["auth"], ("operator", "op3r"))
        load = {s["name"]: s for s in res["panels"]["load"]["series"]}
        self.assertTrue(load["mine"]["own"])
        self.assertFalse(load["theirs"]["own"])
        self.assertEqual(load["theirs"]["account"], "acct_other")
        self.assertEqual(res["panels"]["summary"]["value"]["hosts"], 2)
        self.assertEqual(len(res["panels"]["ingest"]["rows"]), 2)
        self.assertEqual(set(res["alerts"]), {"performance", "security"})

    def test_a_central_that_keeps_its_operator_route_closed_shows_the_account(self):
        with patch(f"{_MODULE}.requests.get",
                   side_effect=_fleet_central(refuse_operator=True)) as get:
            res = self._call()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["scope"], "account")
        account_calls = [c for c in get.call_args_list if "/r/api/v1/" in c.args[0]]
        self.assertEqual(len(account_calls), len(PANELS["fleet"]))
        for call in account_calls:
            self.assertEqual(call.kwargs["auth"], ("acct_test", "s3cr3t"))
        # Read with the account, every host is this panel's: no account
        # names in the legend.
        for series in res["panels"]["load"]["series"]:
            self.assertTrue(series["own"])
            self.assertEqual(series["account"], "")

    def test_an_operator_over_its_cap_is_busy_not_refused(self):
        """vmauth answers 429 past the operator's 16 requests in flight.

        Busy is not a closed route: the board says so rather than
        falling back to the account's servers.
        """
        def answer(url, params, **kw):
            if "/admin-r/" in url:
                return _refusal(429)
            return _fleet_central()(url, params, **kw)

        with patch(f"{_MODULE}.requests.get", side_effect=answer) as get:
            res = self._call()
        self.assertEqual(res["ok"], False)
        self.assertEqual(res["reason"], "busy")
        self.assertIsNone(res["since"])
        self.assertTrue(all("/admin-r/" in c.args[0] for c in get.call_args_list))

    def test_a_panel_without_the_operator_credential_reads_its_account(self):
        self.settings.metrics_operator_token = False
        with patch(f"{_MODULE}.requests.get", side_effect=_fleet_central()) as get:
            res = self._call()
        self.assertEqual(res["scope"], "account")
        self.assertEqual(get.call_count, len(PANELS["fleet"]))
        self.assertTrue(all("/admin-r/" not in c.args[0] for c in get.call_args_list))

    def test_a_central_that_is_not_ours_has_no_operator_route(self):
        """Only a central this panel deployed ends in ``/r``."""
        self.settings.metrics_central_url = "http://vm.test:8428"
        with patch(f"{_MODULE}.requests.get", side_effect=_fleet_central()) as get:
            res = self._call()
        self.assertEqual(res["scope"], "account")
        self.assertTrue(all("/admin-r/" not in c.args[0] for c in get.call_args_list))

    def test_active_alerts_are_counted_by_kind(self):
        with patch(f"{_MODULE}.requests.get", side_effect=_fleet_central()):
            before = self._call()["alerts"]
        Alert = self.env["cloud.alert"].sudo()
        # Codes no policy confirms first: raised on the spot.
        Alert.raise_alert("test_fleet_performance", "Slow", level="warning")
        Alert.raise_alert("security_test_fleet", "SSH", level="warning")
        with patch(f"{_MODULE}.requests.get", side_effect=_fleet_central()):
            after = self._call()["alerts"]
        self.assertEqual(after, {
            "performance": before["performance"] + 1,
            "security": before["security"] + 1,
        })


@tagged("post_install", "-at_install")
class TestHostSilence(TransactionCase):
    """Why the central has nothing on a server, from what the panel keeps."""

    NOW = datetime(2026, 11, 12, 10, 0, 0)

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HSilent2", "ip_address": "203.0.113.72", "user": "root",
            "wildcard_domain": "hsilent2.example.com",
            "metrics_agents_state": "installed",
            "metrics_last_seen": self.NOW - timedelta(minutes=3),
        })

    def _code(self):
        """Return the silence code of the server at ``NOW``."""
        return self.host._metrics_silence(now=self.NOW)["code"]

    def test_the_agents_then_the_readings(self):
        self.assertEqual(self._code(), "window")
        self.host.metrics_agents_state = "failed"
        self.assertEqual(self._code(), "agents_failed")
        self.host.metrics_agents_state = "never"
        self.assertEqual(self._code(), "agents_missing")
        self.host.write({"metrics_agents_state": "installed", "metrics_last_seen": False})
        self.assertEqual(self._code(), "agents_waiting")
        self.host.metrics_last_seen = self.NOW - timedelta(minutes=45)
        self.assertEqual(self._code(), "host_silent")


class TestHostAndFleetPanels(BaseCase):
    """The panels of a server and of the fleet, and how they are shaped."""

    def _panel(self, scope, key):
        """Return the panel *key* of *scope*."""
        return next(p for p in PANELS[scope] if p["key"] == key)

    def test_every_server_expression_selects_by_its_id(self):
        values = {"host_id": 3, "one_off": "x", "window": 120}
        for panel in PANELS["host"]:
            expression = panel_expression(panel, values, 30)
            self.assertIn('host_id="3"', expression, panel["key"])
            self.assertNotIn("instance_id=\"", expression.replace('instance_id!=""', ""))

    def test_the_fleet_keeps_the_account_beside_the_host(self):
        """Read by the operator, ``host_id`` repeats across accounts."""
        values = {"one_off": "x", "window": 120}
        for panel in PANELS["fleet"]:
            expression = panel_expression(panel, values, 30)
            self.assertNotIn("host_id=", expression, panel["key"])
            self.assertIn("ic_account", expression, panel["key"])

    def test_a_stacked_panel_adds_up_what_it_leaves_out(self):
        panel = self._panel("host", "cpu_instances")
        raw = [
            ({"instance_id": str(i), "instance": f"i{i}", "ic_role": "use"},
             [(1.0, float(i)), (31.0, float(i))])
            for i in range(14)
        ]
        series = shape_series(panel, raw, 0, 60)
        self.assertEqual(len(series), _MAX_SERIES)
        others = series[-1]
        self.assertEqual(others["role"], "others")
        # The five quietest: 0 + 1 + 2 + 3 + 4.
        self.assertEqual(others["points"], [[1, 10.0], [31, 10.0]])

    def test_the_table_has_one_row_per_instance(self):
        panel = self._panel("host", "consumers")
        rows = shape_table(panel, [
            ({"instance_id": "5", "instance": "a", "ic_role": "cpu"}, 12.345),
            ({"instance_id": "5", "ic_role": "disk"}, 100.0),
            ({"instance_id": "", "ic_role": "cpu"}, 1.0),
        ])
        self.assertEqual(rows, [{"id": "5", "name": "a", "cpu": 12.35, "disk": 100.0}])

    def test_a_silence_is_a_segment_of_its_own_and_comes_first(self):
        panel = self._panel("fleet", "ingest")
        raw = [
            ({"host": "b", "host_id": "1", "ic_account": "x"},
             [(0.0, 1.0), (30.0, 1.0), (60.0, 1.0)]),
            ({"host": "a", "host_id": "2", "ic_account": "x"},
             [(0.0, 1.0), (60.0, 1.0)]),
        ]
        out = shape_state(panel, raw, 0, 60, 30)
        self.assertEqual([r["name"] for r in out["rows"]], ["a", "b"])
        self.assertEqual(
            [(s["start"], s["end"], s["state"]) for s in out["rows"][0]["segments"]],
            [(0, 30, "ok"), (30, 60, "silent"), (60, 60, "ok")],
        )
        self.assertEqual(out["rows"][1]["segments"], [
            {"start": 0, "end": 60, "state": "ok"},
        ])
        many = [({"host": f"h{i}", "host_id": str(i)}, [(0.0, 1.0)]) for i in range(25)]
        self.assertEqual(shape_state(panel, many, 0, 0, 30)["hidden"], 25 - _MAX_STATE_ROWS)


@tagged("post_install", "-at_install")
class TestHostDiagnosis(TransactionCase):
    """A server's diagnosis in words, from the panels of its board."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HDiag", "ip_address": "203.0.113.73", "user": "root",
            "wildcard_domain": "hdiag.example.com",
        })

    def _board(self, memory=50, root=40, load=0.5, io=1, rows=None):
        """Return a board whose last 15 minutes read these figures."""
        flat = lambda v: [[900, v], [1800, v]]  # noqa: E731
        return {
            "start": 0, "end": 1800, "step": 30,
            "panels": {
                "memory": {"series": [{"role": "used", "name": "used", "points": flat(memory)}]},
                "root": {"series": [{"role": "used", "name": "used", "points": flat(root)}]},
                "load": {"series": [{"role": "load15", "name": "load15", "points": flat(load)}]},
                "pressure": {"series": [{"role": "io", "name": "io", "points": flat(io)}]},
                "host": {"value": {"cores": 4, "memory_total": 8 * _GIB}},
                "consumers": {"rows": rows or []},
            },
        }

    def test_a_calm_server_reads_normal(self):
        diagnosis = diagnose_host(self._board(), self.host, now=1800)
        self.assertEqual(diagnosis["tone"], "ok")
        self.assertEqual([f["code"] for f in diagnosis["findings"]], ["normal"])

    def test_a_full_disk_and_a_saturated_cpu_name_their_instance(self):
        rows = [{"name": "big", "cpu": 360, "memory": _GIB, "disk": 40 * _GIB}]
        diagnosis = diagnose_host(
            self._board(root=95, load=3, rows=rows), self.host, now=1800,
        )
        codes = {f["code"]: f for f in diagnosis["findings"]}
        self.assertEqual(diagnosis["tone"], "bad")
        self.assertIn("big", codes["root_full"]["text"])
        self.assertIn("big", codes["cpu_saturated"]["text"])
        # 360 % of one core on four cores is 90 % of the server.
        self.assertIn("90%", codes["cpu_saturated"]["text"])

    def test_a_zoom_into_the_past_has_no_diagnosis(self):
        diagnosis = diagnose_host(self._board(root=99), self.host, now=86400)
        self.assertFalse(diagnosis["current"])
        self.assertEqual(diagnosis["findings"], [])
