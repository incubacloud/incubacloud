"""Abandoned host-console sessions are reconciled by the route GC.

The host console rejects a second session while one is 'open' for the
same (user, host); before this reconciliation an abandoned session
blocked its user permanently.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestHostSessionReconcile(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "recon-host",
                "ip_address": "192.0.2.20",
                "user": "ubuntu",
                "wildcard_domain": "recon.example.com",
            }
        )
        self.Session = self.env["cloud.host.session"].sudo()
        self.Route = self.env["cloud.host.terminal.route"]

    def test_abandoned_host_session_closes_with_reason(self):
        ses = self.Session.create(
            {
                "host_id": self.host.id,
                "session_id": "dead-host-session",
                "opened_at": fields.Datetime.now() - timedelta(minutes=30),
            }
        )
        self.Route._gc()
        self.assertEqual(ses.state, "closed")
        self.assertEqual(ses.close_reason, "abandoned")
        self.assertTrue(ses.closed_at)

    def test_reconciled_session_unblocks_a_new_console(self):
        """The exact lock the reconciliation exists to break: an open
        row for (user, host) refuses a new console."""
        self.Session.create(
            {
                "host_id": self.host.id,
                "session_id": "dead-host-session-2",
                "opened_at": fields.Datetime.now() - timedelta(minutes=30),
            }
        )
        self.Route._gc()
        blocking = self.Session.search_count(
            [
                ("host_id", "=", self.host.id),
                ("user_id", "=", self.env.user.id),
                ("state", "=", "open"),
            ]
        )
        self.assertEqual(blocking, 0)
