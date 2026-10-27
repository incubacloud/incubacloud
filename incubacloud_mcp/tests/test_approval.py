"""An approval: one user, one tool, one set of arguments, ten minutes, once."""

import threading
import time
from contextlib import closing
from datetime import timedelta

import psycopg2

from odoo import api, fields, sql_db
from odoo.orm.environments import Transaction
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.incubacloud_mcp.models.mcp_approval import token_hash

from .common import McpUsersMixin


@tagged("post_install", "-at_install")
class TestApprovals(McpUsersMixin, TransactionCase):
    """What an approval binds, how long it lasts, and that it is used once."""

    def setUp(self):
        """A manager who asks for approvals."""
        super().setUp()
        self.user = self._cloud_user("mcp-approval-mgr", group="group_cloud_manager")
        self.Approval = self.env["incubacloud.mcp.approval"].with_user(self.user)

    def _open(self, tool="delete_project", digest="h1"):
        """Open a pending approval as the test user."""
        return self.Approval._open(tool, digest, "Delete it.", "text")

    def test_it_opens_pending_for_ten_minutes_keeping_only_the_token_hash(self):
        """A new approval is pending, the user's, ten minutes long, and keeps
        the hash of its page token, never the token."""
        approval, token = self._open()
        self.assertEqual((approval.state, approval.user_id), ("pending", self.user))
        self.assertEqual(approval.token_hash, token_hash(token))
        self.assertNotIn(token, approval.token_hash)
        left = approval.expires_at - fields.Datetime.now()
        self.assertTrue(timedelta(minutes=9) < left <= timedelta(minutes=10))
        self.assertEqual(self.Approval._by_token(token), approval)

    def test_it_is_used_once(self):
        """The first use takes it; the second finds it used."""
        approval, _token = self._open()
        self.assertTrue(approval._consume("pending"))
        self.assertFalse(approval._consume("pending"))
        self.assertEqual(approval.state, "used")

    def test_an_expired_one_cannot_be_used_or_decided(self):
        """Past its ten minutes it can neither be approved nor used."""
        approval, _token = self._open()
        approval.sudo().expires_at = fields.Datetime.now() - timedelta(minutes=1)
        self.assertFalse(approval._decide(True))
        self.assertFalse(approval._consume("pending"))
        self.assertEqual(approval.state, "pending")

    def test_approved_ones_are_found_for_the_same_call_only(self):
        """An approved approval answers its user, tool and arguments only."""
        approval, _token = self._open()
        self.assertTrue(approval._decide(True))
        self.assertEqual(self.Approval._find_approved("delete_project", "h1"), approval)
        self.assertFalse(self.Approval._find_approved("delete_project", "h2"))
        self.assertFalse(self.Approval._find_approved("delete_host", "h1"))
        other = self._cloud_user("mcp-approval-other", group="group_cloud_manager")
        self.assertFalse(
            self.env["incubacloud.mcp.approval"]
            .with_user(other)
            ._find_approved("delete_project", "h1")
        )

    def test_a_cancelled_one_cannot_be_approved(self):
        """Once cancelled, it stays cancelled."""
        approval, _token = self._open()
        self.assertTrue(approval._decide(False))
        self.assertFalse(approval._decide(True))
        self.assertEqual(approval.state, "cancelled")

    def test_a_no_from_the_client_withdraws_even_an_approved_one(self):
        """Withdrawing cancels a pending or an approved approval, never a
        used one."""
        approved, _token = self._open()
        approved._decide(True)
        self.assertTrue(approved._withdraw())
        self.assertEqual(approved.state, "cancelled")
        used, _token = self._open(digest="h2")
        used._consume("pending")
        self.assertFalse(used._withdraw())
        self.assertEqual(used.state, "used")

    def test_the_page_lives_on_the_panel(self):
        """The approval page is on the panel's origin, under its token."""
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.mcp.resource", "https://panel.example.test/mcp"
        )
        approval, token = self._open()
        self.assertEqual(
            approval._approve_url(token),
            f"https://panel.example.test/cloud/mcp/approve/{token}",
        )

    def test_the_vacuum_removes_what_expired_more_than_a_day_ago(self):
        """Approvals expired over a day ago go; recent ones stay."""
        old, _t = self._open()
        recent, _t = self._open(digest="h2")
        old.sudo().expires_at = fields.Datetime.now() - timedelta(days=2)
        recent.sudo().expires_at = fields.Datetime.now() - timedelta(hours=2)
        self.env["incubacloud.mcp.approval"]._gc_expired()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())


@tagged("post_install", "-at_install")
class TestApprovalRace(TransactionCase):
    """Two retries racing for one approval: one runs, the other does not.

    Each retry is a transaction of its own, as two requests are: the
    test commits, on cursors of its own, and removes what it committed.
    """

    def setUp(self):
        """The admin, who asks for the approval."""
        super().setUp()
        self.admin_id = self.env.ref("base.user_admin").id

    def _cursor(self):
        """Return a new cursor on the test database, outside the test's
        transaction."""
        return closing(sql_db.db_connect(self.env.cr.dbname).cursor())

    def _approvals(self, cr):
        """Return the approvals model, as the admin, in the transaction of *cr*."""
        if cr.transaction is None:
            # The test's registry, not one looked up by name: the thread
            # that loaded it may hold the registries' lock while tests run.
            cr.transaction = Transaction(self.registry)
        return api.Environment(cr, self.admin_id, {})["incubacloud.mcp.approval"]

    def _approval(self, cr, approval_id):
        """Return the approval *approval_id* in the transaction of *cr*."""
        return self._approvals(cr).browse(approval_id)

    def _remove(self, approval_id):
        """Delete the committed approval *approval_id*."""
        with self._cursor() as cr:
            cr.execute(
                "DELETE FROM incubacloud_mcp_approval WHERE id = %s", (approval_id,)
            )
            cr.commit()

    def _wait_for_a_lock_wait(self):
        """Wait until a session of the test database waits for a lock."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # A cursor per look: a transaction keeps its first view of
            # pg_stat_activity.
            with self._cursor() as cr:
                cr.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = %s AND wait_event_type = 'Lock'",
                    (self.env.cr.dbname,),
                )
                if cr.fetchone()[0]:
                    return
            time.sleep(0.05)
        self.fail("The second retry never waited for the first.")

    def test_two_retries_at_once_use_it_once(self):
        """The second retry waits for the first, then does not get it.

        Under Odoo's isolation it fails to serialise, and Odoo retries the
        request; the retry finds the approval used.
        """
        with self._cursor() as cr:
            approval, _token = self._approvals(cr)._open(
                "delete_project", "race", "Race.", "text"
            )
            approval._decide(True)
            cr.commit()
            approval_id = approval.id
        self.addCleanup(self._remove, approval_id)
        outcome = {}

        def second():
            """Use the approval in a transaction of its own."""
            with self._cursor() as cr:
                cr.execute("SET lock_timeout = '10s'")
                # The failure to serialise is the expected outcome; Odoo
                # logs the query that met it.
                try:
                    with mute_logger("odoo.sql_db"):
                        outcome["second"] = self._approval(cr, approval_id)._consume(
                            "approved"
                        )
                except psycopg2.errors.SerializationFailure:
                    outcome["second"] = "serialization failure"
                cr.rollback()

        with self._cursor() as first_cr:
            self.assertTrue(self._approval(first_cr, approval_id)._consume("approved"))
            thread = threading.Thread(target=second)
            thread.start()
            self._wait_for_a_lock_wait()
            first_cr.commit()
        thread.join(15)
        self.assertIn(outcome.get("second"), (False, "serialization failure"))
        with self._cursor() as cr:
            retried = self._approval(cr, approval_id)
            self.assertFalse(retried._consume("approved"))
            self.assertEqual(retried.state, "used")
