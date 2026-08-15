"""Tests for deriving ``running`` from metrics (Fase 4 / A8).

The two fail-safes are the point of this file: an unreachable backend
must change nothing, and an instance the backend has never reported on
must not be declared stopped. Both protect the same chain — ``running``
feeds ``sleeping`` feeds ``last_activity_at`` feeds the 14-day
auto-suspend, so a false "stopped" eventually has a billing consequence.
"""
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"


def _samples(*pairs):
    """Build a PromQL payload of (instance_id, seconds-since-seen)."""
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"instance_id": str(iid)}, "value": [0, str(age)]}
                for iid, age in pairs
            ],
        },
    }


class InstanceLivenessCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
        })
        self.project = self.env["cloud.project"].create({"name": "P"})
        self.inst = self.env["cloud.instance"].create({
            "name": "i1", "project_id": self.project.id,
            "environment": "staging",
        })

    def _run(self, payload=None, exc=None):
        resp = MagicMock(spec=requests.Response)
        resp.json.return_value = payload or _samples()
        resp.raise_for_status.return_value = None
        with patch(f"{_MODULE}.requests.get", side_effect=exc,
                   return_value=resp):
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()


class TestLivenessSendsCredentials(InstanceLivenessCase):
    """The liveness query must authenticate as this panel's account.

    ``promql_query`` attaches credentials only when given BOTH halves —
    ``auth=(user, token) if (token and user) else None`` — so a caller
    that unpacks the pair and forwards just the token queries
    anonymously. The central answers 401, the cron logs a warning and
    returns, and liveness silently stops being refreshed: every
    instance keeps whatever ``running`` it last had, which then feeds
    ``sleeping`` and the auto-suspend clock.

    That is precisely what this call site did while the other two
    passed both halves, so the property is pinned on the wire rather
    than on the call.
    """

    def setUp(self):
        super().setUp()
        self.settings.write({
            "metrics_account": "acct_test",
            "metrics_remote_write_token": "s3cr3t",
        })

    def test_the_query_carries_both_halves_of_the_credential(self):
        resp = MagicMock(spec=requests.Response)
        resp.json.return_value = _samples()
        resp.raise_for_status.return_value = None
        with patch(f"{_MODULE}.requests.get", return_value=resp) as get:
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()
        self.assertEqual(
            get.call_args.kwargs["auth"], ("acct_test", "s3cr3t"),
        )


class TestLiveness(InstanceLivenessCase):

    def test_a_recently_seen_container_marks_running(self):
        self.inst.write({"running": False})
        self._run(_samples((self.inst.id, 20)))
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)

    def test_a_stale_container_marks_not_running(self):
        self.inst.write({"running": True})
        self._run(_samples((self.inst.id, 9999)))
        self.inst.invalidate_recordset()
        self.assertFalse(self.inst.running)

    def test_the_window_is_forgiving_of_one_missed_scrape(self):
        """Just over the scrape interval must not flap the flag."""
        window = self.env["cloud.instance"]._metrics_liveness_window()
        self.assertGreaterEqual(window, 90)
        self.inst.write({"running": False})
        self._run(_samples((self.inst.id, 45)))
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)


class TestLivenessFailSafes(InstanceLivenessCase):

    def test_unreachable_backend_changes_nothing(self):
        self.inst.write({"running": True})
        self._run(exc=requests.ConnectionError("down"))
        self.inst.invalidate_recordset()
        self.assertTrue(
            self.inst.running,
            "an unreachable backend must not mark instances stopped",
        )

    def test_an_uncovered_instance_is_left_alone(self):
        """No metrics for this instance = unknown, not stopped."""
        other = self.env["cloud.instance"].create({
            "name": "i2", "project_id": self.project.id,
            "environment": "staging",
        })
        self.inst.write({"running": True})
        other.write({"running": True})
        # Only i1 is reported on, and it is stale.
        self._run(_samples((self.inst.id, 9999)))
        self.inst.invalidate_recordset()
        other.invalidate_recordset()
        self.assertFalse(self.inst.running)
        self.assertTrue(
            other.running,
            "an instance the backend never reported on was clobbered",
        )

    def test_disabled_master_switch_changes_nothing(self):
        self.settings.metrics_enabled = False
        self.inst.write({"running": True})
        self._run(_samples((self.inst.id, 9999)))
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)

    def test_empty_result_changes_nothing(self):
        """A backend with no data yet must not stop the whole fleet."""
        self.inst.write({"running": True})
        self._run(_samples())
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.running)
