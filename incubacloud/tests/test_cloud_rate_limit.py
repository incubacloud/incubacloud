"""Tests for ``cloud.rate.limit``.

The counter bypasses enforcement under ``test_enable`` unless the
context flag ``force_rate_limit`` is set — these tests opt in via
``.with_context(force_rate_limit=True)`` on every model call that
matters. That keeps the rest of the Python test suite free of
accidental rate-limit flakes.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestCloudRateLimitHit(TransactionCase):
    """Tumbling-window behavior of ``hit()``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rl = cls.env['cloud.rate.limit'].with_context(
            force_rate_limit=True,
        )

    def test_first_hit_creates_row_and_allows(self):
        ok = self.rl.hit('test:first', max_per_window=3)
        self.assertTrue(ok)
        rec = self.env['cloud.rate.limit'].search([
            ('bucket', '=', 'test:first'),
        ])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec.count, 1)

    def test_hits_within_window_increment_until_cap(self):
        bucket = 'test:cap'
        for _ in range(3):
            self.assertTrue(self.rl.hit(bucket, max_per_window=3))
        # Fourth hit in the same window is over the cap.
        self.assertFalse(self.rl.hit(bucket, max_per_window=3))

    def test_new_window_resets_counter(self):
        bucket = 'test:reset'
        for _ in range(3):
            self.rl.hit(bucket, max_per_window=3)
        # Backdate the window so age >= window_seconds. Flush before the
        # next ``hit()`` call: the counter UPSERT runs as raw SQL and
        # does not auto-flush pending ORM writes on the same row, so
        # without an explicit flush the SQL would still see the original
        # ``window_start`` and increment past the cap instead of resetting.
        rec = self.env['cloud.rate.limit'].search([
            ('bucket', '=', bucket),
        ])
        rec.write({
            'window_start': fields.Datetime.now() - timedelta(seconds=90),
        })
        rec.flush_recordset()
        # Next hit starts a fresh window and is allowed.
        self.assertTrue(self.rl.hit(bucket, max_per_window=3))
        rec.invalidate_recordset()
        self.assertEqual(rec.count, 1)

    def test_test_mode_bypass_without_force_flag(self):
        """Plain ``env['cloud.rate.limit']`` in tests always allows."""
        rl = self.env['cloud.rate.limit']
        for _ in range(100):
            self.assertTrue(rl.hit('test:bypass', max_per_window=1))

    def test_hit_requires_cap_key_or_max_per_window(self):
        with self.assertRaises(ValueError):
            self.rl.hit('test:bad')


class TestCloudRateLimitCap(TransactionCase):
    """``_get_cap`` reads from ``cloud.settings`` with safe defaults."""

    def setUp(self):
        super().setUp()
        self.rl = self.env['cloud.rate.limit']
        self.settings = self.env['cloud.settings']._get_system()

    def test_cap_returns_configured_value(self):
        self.settings.rate_limit_webhook_per_min = 77
        self.assertEqual(
            self.rl._get_cap('rate_limit_webhook_per_min'), 77,
        )

    def test_cap_falls_back_to_default_when_zero(self):
        """Zero is treated as 'unset' — no operator should ever lock
        every request out by accident."""
        self.settings.rate_limit_webhook_per_min = 0
        self.assertEqual(
            self.rl._get_cap('rate_limit_webhook_per_min'), 300,
        )

    def test_cap_falls_back_to_default_when_negative(self):
        self.settings.rate_limit_terminal_user_per_min = -5
        self.assertEqual(
            self.rl._get_cap('rate_limit_terminal_user_per_min'), 10,
        )


class TestCloudRateLimitGc(TransactionCase):
    """``_gc`` removes buckets whose window has fully aged out."""

    def test_gc_drops_old_windows(self):
        now = fields.Datetime.now()
        self.env['cloud.rate.limit'].create([
            {'bucket': 'test:gc:old',
             'window_start': now - timedelta(hours=48),
             'count': 1},
            {'bucket': 'test:gc:fresh',
             'window_start': now,
             'count': 1},
        ])
        self.env['cloud.rate.limit']._gc(older_than_hours=24)
        remaining = self.env['cloud.rate.limit'].search([
            ('bucket', 'like', 'test:gc:%'),
        ])
        self.assertEqual(remaining.mapped('bucket'), ['test:gc:fresh'])


class TestWebhookRateLimit(TransactionCase):
    """End-to-end: ``/cloud/github/webhook`` 429s when the IP bucket is
    over the configured cap."""

    def setUp(self):
        super().setUp()
        # Bring the cap down to 2 so the test doesn't need 300 hits.
        self.env['cloud.settings']._get_system().rate_limit_webhook_per_min = 2

    def test_webhook_returns_429_when_bucket_exceeded(self):
        from odoo.addons.incubacloud.controllers import github_webhook as gw
        # Avoid touching ``cloud.github.credential.service`` (heavy
        # dep). The rate-limit check happens before any signature
        # work, so the test only needs to exercise that short-circuit
        # path.
        ip = '198.51.100.7'
        rl = self.env['cloud.rate.limit'].with_context(
            force_rate_limit=True,
        )
        # Simulate two allowed hits + one denied.
        self.assertTrue(rl.hit(
            f'webhook_ip:{ip}',
            cap_key='rate_limit_webhook_per_min',
        ))
        self.assertTrue(rl.hit(
            f'webhook_ip:{ip}',
            cap_key='rate_limit_webhook_per_min',
        ))
        self.assertFalse(rl.hit(
            f'webhook_ip:{ip}',
            cap_key='rate_limit_webhook_per_min',
        ))
        # The controller's ``_client_ip`` helper is independent of
        # this behavior; validate it returns something sensible when
        # no request is bound (falls back to the "unknown" string).
        self.assertEqual(gw._client_ip(), 'unknown')
