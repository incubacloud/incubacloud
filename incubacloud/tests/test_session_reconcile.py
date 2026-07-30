"""Tests for abandoned-console-session reconciliation (route GC hook).

Before this existed, the only path to ``state='closed'`` was the
explicit ``/close`` endpoint; a crashed browser left the session open
forever and the one-console-per-target guard locked the user out for
good. The route GC now closes open sessions whose route is gone.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestSessionReconcile(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Recon Proj"})
        self.instance = self.env["cloud.instance"].create(
            {
                "name": "reconinst",
                "project_id": self.project.id,
                "environment": "staging",
            }
        )
        self.Session = self.env["cloud.instance.session"].sudo()
        self.Route = self.env["cloud.terminal.route"]

    def _session(self, sid, minutes_old):
        return self.Session.create(
            {
                "instance_id": self.instance.id,
                "session_id": sid,
                "opened_at": fields.Datetime.now()
                - timedelta(minutes=minutes_old),
            }
        )

    def test_abandoned_session_is_closed_by_gc(self):
        ses = self._session("dead-session-1", minutes_old=30)
        self.Route._gc()
        self.assertEqual(ses.state, "closed")
        self.assertTrue(ses.closed_at)

    def test_recent_session_survives_the_grace_window(self):
        """A session younger than the grace window is left alone even
        without a route — its route may simply not be visible yet."""
        ses = self._session("fresh-session-1", minutes_old=1)
        self.Route._gc()
        self.assertEqual(ses.state, "open")

    def test_session_with_live_route_stays_open(self):
        import os

        ses = self._session("live-session-1", minutes_old=30)
        self.Route.sudo().create(
            {
                "session_id": "live-session-1",
                "pid": os.getpid(),
                "port": 45678,
                "auth_token": "tok",
                "user_id": self.env.user.id,
            }
        )
        self.Route._gc()
        self.assertEqual(ses.state, "open")

    def test_closed_sessions_are_untouched(self):
        ses = self._session("closed-session-1", minutes_old=30)
        closed_at = fields.Datetime.now() - timedelta(minutes=20)
        ses.write({"state": "closed", "closed_at": closed_at})
        self.Route._gc()
        self.assertEqual(ses.closed_at, closed_at)
