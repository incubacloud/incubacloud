"""A hit on an hourly quota bucket survives another hit landing at once.

The repository editor fires three reads at once and all three add to the
user's one ``git_reads_user:<uid>`` row. Odoo opens a cursor at REPEATABLE
READ, so a hit that has already read the cap (``_get_cap`` is the first
query of its transaction) and then meets a row another hit has just
created or committed is aborted with ``could not serialize access due to
concurrent update``. PostgreSQL's error reaches Odoo's SQL layer, which
logs it at ERROR before Odoo retries the request: it ends well, up to two
seconds later and with a line in the log for every new bucket.

The transactions are real: the hit under test and its rival each run on a
cursor of their own, and the rival commits between the moment the hit
takes its snapshot and the moment it writes. That is the one interleaving
that matters, made certain instead of left to a thread race (a thread
cannot build an environment while the module update holds the registry).
"""
from unittest.mock import patch

from odoo import api
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models._repo_requirements import (
    consume_hourly_quota,
)
from odoo.addons.incubacloud.models.cloud_rate_limit import CloudRateLimit

_BUCKET = "quota_concurrency_test:1"
_CAP_KEY = "rate_limit_git_reads_per_hour"


class TestHourlyQuotaConcurrency(TransactionCase):
    """A hit racing a committed rival on one bucket: counted, never aborted."""

    def setUp(self):
        """Enable the limiter and start from a bucket that does not exist."""
        super().setUp()
        self.route_env = self.env(
            context=self.env.context | {"force_rate_limit": True},
        )
        self._clear_bucket()
        self.addCleanup(self._clear_bucket)

    def _clear_bucket(self):
        """Delete the independently committed counter of the test."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM cloud_rate_limit WHERE bucket = %s", (_BUCKET,),
            )
            cr.commit()

    def _stored_count(self):
        """Return the count the bucket holds once the hits committed."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT count FROM cloud_rate_limit WHERE bucket = %s",
                (_BUCKET,),
            )
            row = cr.fetchone()
            return row[0] if row else None

    def _hit_with_rival(self, cap):
        """Make a hit while a rival hit commits during its snapshot.

        The rival is another hit on the same bucket, on a cursor of its
        own, committed right after the hit under test read the cap: the
        snapshot of the hit under test is already taken and does not see
        the rival's row.

        :param int cap: the cap both hits are held to
        :return: ``(rival, mine)``: whether each hit was within the cap
        :rtype: tuple
        """
        original = CloudRateLimit._get_cap
        rival = []

        def cap_while_rival_commits(model, key):
            """Read the cap, then let the rival hit and commit, once."""
            original(model, key)
            if not rival:
                with model.env.registry.cursor() as cr:
                    rival_env = api.Environment(
                        cr, model.env.uid, dict(model.env.context),
                    )
                    rival.append(rival_env['cloud.rate.limit'].sudo().hit(
                        _BUCKET, max_per_window=cap,
                    ))
                    cr.commit()
            return cap

        with patch.object(
            CloudRateLimit, "_get_cap", autospec=True,
            side_effect=cap_while_rival_commits,
        ):
            mine = consume_hourly_quota(self.route_env, _BUCKET, _CAP_KEY)
        return rival[0], mine

    def test_hit_on_a_new_bucket_counts_beside_a_committed_rival(self):
        """The hit does not abort on the row the rival created, and adds to it."""
        rival, mine = self._hit_with_rival(cap=5)
        self.assertTrue(rival)
        self.assertTrue(mine)
        self.assertEqual(self._stored_count(), 2)

    def test_hit_on_an_existing_bucket_counts_beside_a_committed_rival(self):
        """The same holds for a row both hits update, not create."""
        self.assertTrue(consume_hourly_quota(
            self.route_env, _BUCKET, _CAP_KEY,
        ))
        rival, mine = self._hit_with_rival(cap=5)
        self.assertTrue(rival)
        self.assertTrue(mine)
        self.assertEqual(self._stored_count(), 3)

    def test_two_hits_at_once_do_not_pass_the_cap(self):
        """With room for one, the rival takes it and the hit is refused."""
        rival, mine = self._hit_with_rival(cap=1)
        self.assertEqual([rival, mine], [True, False])
        self.assertEqual(self._stored_count(), 2)
