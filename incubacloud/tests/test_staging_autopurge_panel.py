"""What the panel is told about an expiry, and the one click that undoes it.

Two warnings are only enough notice if answering them is trivial, so the
countdown has to reach the screen and Keep has to reset the whole ladder
from one press. The failure worth guarding against here is the one the
pull-request switch already produced once: a field that exists on the
model, is written correctly, and never travels through the CRUD — so the
feature looks implemented and is invisible.

The countdown is also deliberately absent until the first warning. A
number ticking down on every staging from the day it is created is
something people learn to ignore, which is exactly the reflex the
warning needs them not to have.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud

_WINDOW = 90


class _PanelCase(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "pn-host",
            "ip_address": "10.0.15.1",
            "user": "root",
            "wildcard_domain": "pn.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "pn-proj"})
        self.staging = self.env["cloud.instance"].create({
            "name": "pn-staging",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.settings.staging_autopurge_days = _WINDOW
        self.controller = _routes_crud.CrudMixin()

    def _request(self):
        """A request bound to this test's env, as the controllers expect."""
        request = MagicMock(spec=Request)
        request.env = self.env
        return request

    def _warned_with(self, days_left):
        """Put the instance on the ladder with *days_left* remaining."""
        idle = _WINDOW - days_left
        self.staging.sudo().write({
            "last_touched_at": fields.Datetime.now() - timedelta(days=idle),
            "autopurge_warned_at": fields.Datetime.now(),
        })


class TestTheCountdownOnlyShowsOnceWarned(_PanelCase):

    def test_a_fresh_staging_shows_nothing(self):
        self.assertIs(self.staging._autopurge_days_left(), False)

    def test_an_idle_but_unwarned_staging_shows_nothing(self):
        """The cron has not spoken yet, so neither does the panel."""
        self.staging.sudo().write({
            "last_touched_at": fields.Datetime.now() - timedelta(days=85),
        })
        self.assertIs(self.staging._autopurge_days_left(), False)

    def test_a_warned_staging_shows_its_days(self):
        self._warned_with(5)
        self.assertEqual(self.staging._autopurge_days_left(), 5)

    def test_an_overdue_staging_shows_zero_not_a_negative(self):
        self._warned_with(-20)
        self.assertEqual(self.staging._autopurge_days_left(), 0)

    def test_an_exempt_staging_shows_nothing(self):
        self._warned_with(5)
        self.staging.sudo().write({"autopurge_exempt": True})
        self.assertIs(self.staging._autopurge_days_left(), False)

    def test_production_shows_nothing(self):
        prod = self.env["cloud.instance"].create({
            "name": "pn-prod",
            "project_id": self.env["cloud.project"].create(
                {"name": "pn-proj-2"},
            ).id,
            "environment": "production",
            "host_id": self.host.id,
        })
        prod.sudo().write({"autopurge_warned_at": fields.Datetime.now()})
        self.assertIs(prod._autopurge_days_left(), False)

    def test_nothing_shows_while_the_window_is_off(self):
        self._warned_with(5)
        self.settings.staging_autopurge_days = 0
        self.assertIs(self.staging._autopurge_days_left(), False)


class TestItTravelsThroughTheCrud(_PanelCase):
    """The failure mode the PR-preview switch already produced once."""

    def _detail(self):
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        with patch.object(_routes_crud, "request", self._request()):
            return self.controller._serialize_instance(self.staging)

    def test_the_countdown_reaches_the_instance_detail(self):
        self._warned_with(7)
        self.assertEqual(self._detail()["autopurge_days_left"], 7)

    def test_the_exemption_reaches_the_instance_detail(self):
        self.staging.sudo().write({"autopurge_exempt": True})
        self.assertTrue(self._detail()["autopurge_exempt"])

    def test_the_exemption_can_be_written_back(self):
        self.assertIn(
            "autopurge_exempt",
            self.controller._SAVE_INSTANCE_ALLOWED,
            "a switch the panel can show and not save is worse than none",
        )


class TestKeep(_PanelCase):

    def _keep(self, instance=None):
        inst = instance or self.staging
        sec = MagicMock()
        self.controller._sec = lambda: sec
        with patch.object(_routes_crud, "request", self._request()):
            result = self.controller.cloud_keep_instance(inst.id)
        return result, sec

    def test_keeping_clears_the_countdown(self):
        self._warned_with(2)
        result, _sec = self._keep()
        self.assertTrue(result["ok"])
        self.assertIs(result["days_left"], False)

    def test_keeping_is_consultant_gated(self):
        """Same role that can deploy an instance can keep one."""
        self._warned_with(2)
        _result, sec = self._keep()
        sec._check_cloud_group.assert_called_once_with(
            "group_cloud_consultant",
        )

    def test_keeping_resets_the_whole_ladder(self):
        self._warned_with(2)
        self.staging.sudo().write({
            "autopurge_final_warned_at": fields.Datetime.now(),
        })
        self._keep()
        self.assertFalse(self.staging.autopurge_warned_at)
        self.assertFalse(self.staging.autopurge_final_warned_at)

    def test_keeping_is_recorded_in_the_audit_log(self):
        self._warned_with(2)
        self._keep()
        entry = self.env["cloud.audit.log"].sudo().search([
            ("action", "=", "Keep instance"),
            ("instance_id", "=", self.staging.id),
        ], limit=1)
        self.assertTrue(entry)
        self.assertIn("pn-staging", entry.details)

    def test_keeping_production_is_refused(self):
        """Nothing to keep: production does not expire."""
        prod = self.env["cloud.instance"].create({
            "name": "pn-prod-2",
            "project_id": self.env["cloud.project"].create(
                {"name": "pn-proj-3"},
            ).id,
            "environment": "production",
            "host_id": self.host.id,
        })
        result, _sec = self._keep(instance=prod)
        self.assertFalse(result["ok"])

    def test_keeping_something_that_is_gone_is_refused(self):
        inst_id = self.staging.id
        self.staging.sudo().unlink()
        sec = MagicMock()
        self.controller._sec = lambda: sec
        with patch.object(_routes_crud, "request", self._request()):
            result = self.controller.cloud_keep_instance(inst_id)
        self.assertFalse(result["ok"])
