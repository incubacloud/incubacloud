"""Two deliveries of the same push on real transactions (G3, §4.7.3).

The receiver relies on the partial unique indexes of
``cloud.gitlab.event``, not on row locks. A delivery whose snapshot was
taken before a twin committed still meets the twin's index entry and is
refused; a twin that rolled back leaves nothing behind, so the next
delivery goes through. Each delivery runs on a cursor of its own, as two
HTTP requests would.
"""
from odoo import api
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger
from psycopg2 import errors as pg_errors

from ._gitlab_hooks import push_body


class TestGitLabReceiverRealCursors(TransactionCase):

    def setUp(self):
        """Commit a provider and a webhook of their own; removed on cleanup."""
        super().setUp()
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, {})
            env["ir.config_parameter"].sudo().set_param(
                "incubacloud.platform_addresses", "192.0.2.1",
            )
            provider = env["cloud.git.provider"].create({
                "name": "GL real", "base_url": "https://gitlab.real-cursors.test",
            })
            self.hook_id = env["cloud.gitlab.hook"].create({
                "provider_id": provider.id, "scope": "project", "path": "acme/rc",
            }).id
            self.provider_id = provider.id
            cr.commit()
        self.addCleanup(self._drop)

    def _drop(self):
        """Delete what the test committed."""
        with self.env.registry.cursor() as cr:
            cr.execute("DELETE FROM cloud_gitlab_event WHERE hook_id = %s", (self.hook_id,))
            cr.execute("DELETE FROM cloud_gitlab_hook WHERE id = %s", (self.hook_id,))
            cr.execute("DELETE FROM cloud_git_provider WHERE id = %s", (self.provider_id,))
            cr.commit()

    def _deliver(self, cr, key, body):
        """Record a delivery on *cr*, as the receiver does.

        :rtype: cloud.gitlab.event
        """
        env = api.Environment(cr, self.env.uid, {})
        hook = env["cloud.gitlab.hook"].sudo().browse(self.hook_id)
        return env["cloud.gitlab.event"].sudo()._record_delivery(
            hook, "Push Hook", key, body,
        )

    def _count(self):
        """Return how many events the webhook holds, committed.

        :rtype: int
        """
        with self.env.registry.cursor() as cr:
            cr.execute("SELECT count(*) FROM cloud_gitlab_event WHERE hook_id = %s", (self.hook_id,))
            return cr.fetchone()[0]

    def test_a_twin_committed_after_the_snapshot_is_still_refused(self):
        body = push_body(path="acme/rc")
        with self.env.registry.cursor() as late:
            late.execute("SELECT 1")  # the snapshot is taken here
            with self.env.registry.cursor() as first:
                self._deliver(first, "same", body)
                first.commit()
            with mute_logger("odoo.sql_db"), self.assertRaises(pg_errors.UniqueViolation):
                self._deliver(late, "same", body)
            late.rollback()
        self.assertEqual(self._count(), 1)

    def test_a_twin_rolled_back_leaves_nothing_behind(self):
        body = push_body(path="acme/rc")
        with self.env.registry.cursor() as first:
            self._deliver(first, "same", body)
            first.rollback()
        with self.env.registry.cursor() as second:
            self._deliver(second, "same", body)
            second.commit()
        self.assertEqual(self._count(), 1)
