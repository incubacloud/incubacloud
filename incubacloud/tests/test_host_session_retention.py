"""The host-shell audit is kept for its retention window, then deleted.

``cloud.host.session`` rows are immutable and no role below the system
one may delete them; the daily vacuum is the one exception. It deletes
closed sessions older than ``host_session_retention_days`` and leaves
open ones alone whatever their age.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud


class TestHostSessionRetention(TransactionCase):
    """What the vacuum deletes, and what it keeps."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "retention-host",
            "ip_address": "192.0.2.31",
            "user": "ubuntu",
            "wildcard_domain": "retention.example.com",
        })
        self.Session = self.env["cloud.host.session"].sudo()
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.now = fields.Datetime.now()

    def _session(self, name, opened_days_ago, closed_days_ago=None):
        """Create a session opened *opened_days_ago* days ago.

        :param name: its ``session_id``
        :param opened_days_ago: days since it was opened
        :param closed_days_ago: days since it was closed; ``None`` leaves
            it open
        :returns: the ``cloud.host.session`` record
        """
        vals = {
            "host_id": self.host.id,
            "session_id": name,
            "opened_at": self.now - timedelta(days=opened_days_ago),
        }
        if closed_days_ago is not None:
            vals.update({
                "state": "closed",
                "closed_at": self.now - timedelta(days=closed_days_ago),
                "close_reason": "user",
            })
        return self.Session.create(vals)

    def test_the_default_is_a_year(self):
        """A new panel keeps the console audit for 365 days."""
        field = self.env["cloud.settings"]._fields["host_session_retention_days"]
        self.assertEqual(field.default(self.env["cloud.settings"]), 365)

    def test_an_old_closed_session_is_deleted(self):
        """Closed past the window: the vacuum deletes it."""
        self.settings.host_session_retention_days = 365
        old = self._session("old-closed", 401, 400)
        self.Session._gc_expired_sessions()
        self.assertFalse(old.exists())

    def test_a_recent_closed_session_is_kept(self):
        """Closed inside the window: it stays."""
        self.settings.host_session_retention_days = 365
        recent = self._session("recent-closed", 31, 30)
        self.Session._gc_expired_sessions()
        self.assertTrue(recent.exists())

    def test_an_old_open_session_is_kept(self):
        """Open, however old: the vacuum never touches it."""
        self.settings.host_session_retention_days = 365
        still_open = self._session("old-open", 500)
        self.Session._gc_expired_sessions()
        self.assertTrue(still_open.exists())
        self.assertEqual(still_open.state, "open")

    def test_the_setting_moves_the_window(self):
        """A shorter window deletes what the default would keep; 0 keeps all."""
        self.settings.host_session_retention_days = 0
        session = self._session("forty-days", 41, 40)
        self.Session._gc_expired_sessions()
        self.assertTrue(session.exists())
        self.settings.host_session_retention_days = 60
        self.Session._gc_expired_sessions()
        self.assertTrue(session.exists())
        self.settings.host_session_retention_days = 30
        self.Session._gc_expired_sessions()
        self.assertFalse(session.exists())

    def test_the_vacuum_is_registered(self):
        """The daily vacuum calls the purge."""
        method = type(self.Session)._gc_expired_sessions
        self.assertTrue(getattr(method, "_autovacuum", False))


class TestHostSessionRetentionSetting(TransactionCase):
    """The window travels through the general-settings endpoints."""

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.settings = self.env["cloud.settings"].sudo()._get()

    def _call(self, method, **kwargs):
        """Call the endpoint *method* with a stand-in request.

        :param method: the name of the controller method
        :returns: what the endpoint answered
        """
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_crud, "request", fake_req):
            return getattr(self.controller, method)(**kwargs)

    def test_get_returns_the_window(self):
        """Settings reads the value held."""
        self.settings.host_session_retention_days = 200
        data = self._call("cloud_get_general_settings")
        self.assertEqual(data["host_session_retention_days"], 200)

    def test_save_writes_the_window_and_floors_it_at_zero(self):
        """Settings writes it; a negative value is read as 0, keep all."""
        result = self._call(
            "cloud_save_general_settings", host_session_retention_days=500,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.settings.host_session_retention_days, 500)
        self._call(
            "cloud_save_general_settings", host_session_retention_days=-3,
        )
        self.assertEqual(self.settings.host_session_retention_days, 0)
