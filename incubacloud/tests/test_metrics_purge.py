"""Series of removed instances and hosts are deleted, later, by account.

What these pin, in order of how badly each would hurt:

  * a selector never leaves the account out — ``instance_id="5"`` exists
    in every account sharing the central, so a selector by id alone
    deletes the instance 5 of all of them;
  * a removal that rolls back deletes nothing — the purge is queued in
    the removal's own transaction;
  * the deletion waits (measured: a removed instance kept sending
    samples for three minutes, and a late sample recreates a deleted
    series);
  * a panel that cannot delete queues nothing, and a deletion that
    fails warns instead of failing anything.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.tests.common import BaseCase, TransactionCase

from ..models.cloud_settings_metrics_purge import (
    PURGE_DELAY_MINUTES,
    PURGE_FAILED_CODE,
    series_selector,
)

_JOB_METHOD = "_job_purge_metrics"
_POST = "odoo.addons.incubacloud.models.cloud_settings_metrics_purge.requests.post"


class TestSeriesSelector(BaseCase):
    """The one function that builds a deletion selector."""

    def test_an_instance_is_selected_with_its_account(self):
        self.assertEqual(
            series_selector("acct_1a", instance_id=5),
            '{ic_account="acct_1a",instance_id="5"}',
        )

    def test_a_host_keeps_the_history_of_instances_it_carried(self):
        """``instance_id=""`` matches only series without that label.

        Measured (lab, 2026-09-27): it deleted the host's own series and
        left one carrying an instance id — the history of an instance
        that moved elsewhere.
        """
        self.assertEqual(
            series_selector("acct_1a", host_id=7),
            '{ic_account="acct_1a",host_id="7",instance_id=""}',
        )

    def test_a_whole_account_must_be_asked_for_explicitly(self):
        self.assertEqual(
            series_selector("acct_1a", whole_account=True),
            '{ic_account="acct_1a"}',
        )

    def test_no_account_no_selector(self):
        """Without it, the selector reaches every account on the central."""
        for account in (None, "", False, 5):
            with self.assertRaises(ValueError, msg=repr(account)):
                series_selector(account, instance_id=5)

    def test_an_account_cannot_widen_the_selector(self):
        """A quote in the value would let it close the matcher early."""
        for account in (
            'acct_1",instance_id=~".*', "acct 1", 'acct_1"', "acct_1}",
        ):
            with self.assertRaises(ValueError, msg=account):
                series_selector(account, instance_id=5)

    def test_exactly_one_target(self):
        with self.assertRaises(ValueError):
            series_selector("acct_1a")
        with self.assertRaises(ValueError):
            series_selector("acct_1a", instance_id=5, host_id=7)
        with self.assertRaises(ValueError):
            series_selector("acct_1a", instance_id=5, whole_account=True)

    def test_ids_are_positive_integers(self):
        for value in ("5", True, 0, -1, 5.0, '5"} or {x="'):
            with self.assertRaises(ValueError, msg=repr(value)):
                series_selector("acct_1a", instance_id=value)


class _PurgeCase(TransactionCase):
    """A panel that owns its central and can delete on it."""

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://172.17.0.1:8428/r",
            "metrics_account": "acct_self01",
            "metrics_operator_token": "op-token",
            "metrics_delete_auth_key": "del-key",
        })
        self.host = self.env["cloud.host"].create({
            "name": "PH", "ip_address": "10.0.0.77", "user": "root",
            "wildcard_domain": "ph.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "PP"})
        self.instance = self.env["cloud.instance"].create({
            "name": "pi", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
        })
        self._before = set(self._jobs().ids)

    def _jobs(self):
        """Return every queued purge job."""
        return self.env["queue.job"].sudo().search(
            [("method_name", "=", _JOB_METHOD)],
        )

    def _new_jobs(self):
        """Return the purge jobs queued since ``setUp``."""
        return self._jobs().filtered(lambda j: j.id not in self._before)


class TestRemovalsQueueTheirPurge(_PurgeCase):

    def test_an_unlinked_instance_queues_one_deferred_purge(self):
        instance_id = self.instance.id
        before = fields.Datetime.now()
        self.instance.unlink()
        job = self._new_jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(
            job.args, [[f'{{ic_account="acct_self01",instance_id="{instance_id}"}}']],
        )
        self.assertGreaterEqual(
            job.eta, before + timedelta(minutes=PURGE_DELAY_MINUTES - 1),
        )
        self.assertEqual(job.channel, "root.bg")

    def test_a_rolled_back_unlink_queues_nothing(self):
        """The record survives, so its series must too."""
        with self.assertRaises(RuntimeError):
            with self.env.cr.savepoint():
                self.instance.unlink()
                raise RuntimeError("the removal failed after the unlink")
        self.assertTrue(self.instance.exists())
        self.assertFalse(self._new_jobs())

    def test_a_retired_host_queues_its_purge(self):
        self.instance.unlink()
        self._before = set(self._jobs().ids)
        self.host.write({"active": False})
        job = self._new_jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(
            job.args,
            [[f'{{ic_account="acct_self01",host_id="{self.host.id}",instance_id=""}}']],
        )

    def test_a_host_retired_earlier_is_not_purged_again_on_unlink(self):
        self.instance.unlink()
        self.host.write({"active": False})
        self._before = set(self._jobs().ids)
        self.host.unlink()
        self.assertFalse(self._new_jobs())

    def test_an_active_host_unlinked_queues_its_purge(self):
        self.instance.unlink()
        self._before = set(self._jobs().ids)
        self.host.unlink()
        self.assertEqual(len(self._new_jobs()), 1)

    def test_a_panel_that_cannot_delete_queues_nothing(self):
        """Each of these means the deletion route does not exist here."""
        for vals in (
            {"metrics_enabled": False},
            {"metrics_operator_token": False},
            # A central deployed before deletions existed.
            {"metrics_delete_auth_key": False},
            # A backend that is not a central of ours.
            {"metrics_central_url": "http://vm.example.com:8428"},
        ):
            with self.subTest(vals=vals), self.env.cr.savepoint() as sp:
                self.settings.write(vals)
                instance = self.env["cloud.instance"].create({
                    "name": "pj", "project_id": self.project.id,
                    "environment": "staging",
                })
                instance.unlink()
                self.assertFalse(self._new_jobs())
                # Back to a panel that can delete, for the next case.
                sp.close(rollback=True)

    def test_a_panel_without_an_account_queues_nothing(self):
        self.settings.metrics_account = False
        self.instance.unlink()
        self.assertFalse(self._new_jobs())


class TestThePurgeJob(_PurgeCase):

    _SELECTORS = [
        '{ic_account="acct_self01",instance_id="5"}',
        '{ic_account="acct_self01",host_id="7",instance_id=""}',
    ]

    def _response(self, status_error=None):
        """Return a spec'd response whose ``raise_for_status`` may raise."""
        response = MagicMock(spec=requests.Response)
        if status_error:
            response.raise_for_status.side_effect = status_error
        return response

    def _alert(self):
        """Return the active purge-failure alert, if any."""
        return self.env["cloud.alert"].sudo().search(
            [("code", "=", PURGE_FAILED_CODE), ("state", "=", "active")],
        )

    def test_one_request_carries_every_selector_through_the_operator_route(self):
        """One call: VictoriaMetrics deletes what matches any ``match[]``."""
        with patch(_POST, return_value=self._response()) as post:
            self.settings._job_purge_metrics(self._SELECTORS)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "http://172.17.0.1:8428/admin-d/api/v1/admin/tsdb/delete_series",
        )
        self.assertEqual(
            kwargs["data"], [("match[]", s) for s in self._SELECTORS],
        )
        self.assertEqual(kwargs["auth"], ("operator", "op-token"))
        self.assertTrue(kwargs["timeout"])

    def test_the_panel_never_sends_the_deletion_key(self):
        """The gateway adds it on the operator route; it stays on disk there."""
        with patch(_POST, return_value=self._response()) as post:
            self.settings._job_purge_metrics(self._SELECTORS)
        self.assertNotIn("del-key", repr(post.call_args))

    def test_an_unreachable_central_warns_and_does_not_raise(self):
        with patch(_POST, side_effect=requests.ConnectionError("down")):
            self.settings._job_purge_metrics(self._SELECTORS)
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")

    def test_a_refused_deletion_warns(self):
        refused = self._response(requests.HTTPError("401 Unauthorized"))
        with patch(_POST, return_value=refused):
            self.settings._job_purge_metrics(self._SELECTORS)
        self.assertTrue(self._alert())

    def test_a_successful_deletion_clears_the_warning(self):
        with patch(_POST, side_effect=requests.ConnectionError("down")):
            self.settings._job_purge_metrics(self._SELECTORS)
        with patch(_POST, return_value=self._response()):
            self.settings._job_purge_metrics(self._SELECTORS)
        self.assertFalse(self._alert())

    def test_a_panel_that_can_no_longer_delete_sends_nothing(self):
        """Observability switched off between queuing and running."""
        self.settings.metrics_enabled = False
        with patch(_POST) as post:
            self.settings._job_purge_metrics(self._SELECTORS)
        post.assert_not_called()
