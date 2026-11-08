"""A hit on a quota counter survives another hit landing at once.

An agent calls the MCP tools in parallel, and the calls count on the same
``cloud_rate_limit`` row (a user's terminal bucket, the user's MCP bucket,
an address's failures); the repository editor's GitHub reads do too. Odoo
opens a cursor at REPEATABLE READ, so a hit whose snapshot is already
taken and that then meets a row another hit has just created or
committed is aborted with ``could not serialize access due to concurrent
update``: Odoo's SQL layer logs it at ERROR and the tool call fails.

The transactions are real: the hit under test and its rival each run on a
cursor of their own, and the rival commits after the hit under test took
its snapshot and before it writes. That is the one interleaving that
matters, made certain instead of left to a thread race (a thread cannot
build an environment while the module update holds the registry).
"""
from unittest.mock import patch

from odoo import api
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models._repo_requirements import (
    consume_hourly_quota,
)
from odoo.addons.incubacloud.models.cloud_rate_limit import CloudRateLimit
from odoo.addons.incubacloud_mcp.models import mcp_auth

_IP = "198.51.100.77"
_CAP_KEY = "rate_limit_terminal_user_per_min"
_GITHUB_CAP_KEY = "rate_limit_github_previews_per_hour"


class TestQuotaConcurrency(TransactionCase):
    """A hit racing a committed rival on one bucket: counted, never aborted."""

    def setUp(self):
        """Enable the limiter and list the counting calls with their buckets."""
        super().setUp()
        self.route_env = self.env(
            context=self.env.context | {"force_rate_limit": True},
        )
        tools = self.route_env["incubacloud.mcp.tools"]
        auth = self.route_env["incubacloud.mcp.auth"]
        self.callers = {
            "mcp tool, cap from settings": (
                "quota_concurrency_test:tool",
                lambda cap: tools._consume_quota(
                    "quota_concurrency_test:tool", cap_key=_CAP_KEY,
                ),
            ),
            "mcp tool, literal cap": (
                "quota_concurrency_test:literal",
                lambda cap: tools._consume_quota(
                    "quota_concurrency_test:literal", max_per_window=cap,
                ),
            ),
            "github quota": (
                "quota_concurrency_test:github",
                lambda cap: consume_hourly_quota(
                    self.route_env,
                    "quota_concurrency_test:github",
                    _GITHUB_CAP_KEY,
                ),
            ),
            "authentication failures": (
                auth._failure_bucket(_IP),
                lambda cap: auth._count_failure(_IP),
            ),
        }
        self._clear_buckets()
        self.addCleanup(self._clear_buckets)

    def _clear_buckets(self):
        """Delete the independently committed counters of the test."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM cloud_rate_limit WHERE bucket = ANY(%s)",
                ([bucket for bucket, _call in self.callers.values()],),
            )
            cr.commit()

    def _stored_count(self, bucket):
        """Return the count the bucket holds once the hits committed."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT count FROM cloud_rate_limit WHERE bucket = %s",
                (bucket,),
            )
            row = cr.fetchone()
            return row[0] if row else None

    def _call_with_rival(self, bucket, call, cap):
        """Make *call* while a rival hit commits during its snapshot.

        The rival is another hit on the same bucket, on a cursor of its
        own, committed once the hit under test has read from the counters'
        table: its snapshot is taken and does not see the rival's row, or
        the rival's update of it.

        :param str bucket: the bucket *call* counts on
        :param call: a function of the cap that makes the hit
        :param int cap: the cap both hits are held to
        :return: ``(rival, mine)``: whether each hit was within the cap
        :rtype: tuple
        """
        original = CloudRateLimit.hit
        rival = []

        def hit_while_rival_commits(model, hit_bucket, **kwargs):
            """Take the snapshot, let the rival hit and commit once, then hit."""
            if not rival:
                rival.append(None)
                model.env.cr.execute(
                    "SELECT count(*) FROM cloud_rate_limit WHERE bucket = %s",
                    (hit_bucket,),
                )
                model.env.cr.fetchone()
                with model.env.registry.cursor() as cr:
                    rival_env = api.Environment(
                        cr, model.env.uid, dict(model.env.context),
                    )
                    rival[0] = original(
                        rival_env["cloud.rate.limit"].sudo(),
                        hit_bucket,
                        max_per_window=cap,
                        window_seconds=kwargs.get("window_seconds", 60),
                    )
                    cr.commit()
            return original(model, hit_bucket, **kwargs)

        with (
            patch.object(
                CloudRateLimit, "hit", autospec=True,
                side_effect=hit_while_rival_commits,
            ),
            patch.object(
                CloudRateLimit, "_get_cap", autospec=True, return_value=cap,
            ),
            patch.object(mcp_auth, "AUTH_FAILURES_PER_MIN", cap),
        ):
            mine = call(cap)
        return rival[0], mine

    def test_hit_on_a_new_bucket_counts_beside_a_committed_rival(self):
        """The hit does not abort on the row the rival created, and adds to it."""
        for label, (bucket, call) in self.callers.items():
            with self.subTest(label):
                self._clear_buckets()
                rival, mine = self._call_with_rival(bucket, call, cap=5)
                self.assertTrue(rival)
                self.assertTrue(mine)
                self.assertEqual(self._stored_count(bucket), 2)

    def test_hit_on_an_existing_bucket_counts_beside_a_committed_rival(self):
        """The same holds for a row both hits update, not create."""
        for label, (bucket, call) in self.callers.items():
            with self.subTest(label):
                self._clear_buckets()
                with patch.object(
                    CloudRateLimit, "_get_cap", autospec=True, return_value=5,
                ):
                    self.assertTrue(call(5))
                rival, mine = self._call_with_rival(bucket, call, cap=5)
                self.assertTrue(rival)
                self.assertTrue(mine)
                self.assertEqual(self._stored_count(bucket), 3)

    def test_two_hits_at_once_do_not_pass_the_cap(self):
        """With room for one, the rival takes it and the hit is refused."""
        for label, (bucket, call) in self.callers.items():
            with self.subTest(label):
                self._clear_buckets()
                rival, mine = self._call_with_rival(bucket, call, cap=1)
                self.assertEqual([rival, mine], [True, False])
                self.assertEqual(self._stored_count(bucket), 2)
