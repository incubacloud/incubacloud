"""Lightweight rate-limit counter backed by the DB.

One row per bucket (e.g. ``webhook_ip:1.2.3.4`` or
``terminal_user:42``), tumbling window (default 60s). Exceeded buckets
return ``False`` from ``hit()`` so callers can short-circuit without
raising.

Caps are read from ``cloud.settings`` so the admin can tune them from
Settings → Rates without redeploying:

  * ``rate_limit_webhook_per_min``        (default 300)
  * ``rate_limit_terminal_per_min``       (default 30)
  * ``rate_limit_terminal_user_per_min``  (default 10)

This is a deliberate copy of ``saas.rate.limit`` (lives in
``incubacloud_saas_manager``) rather than a shared base. The SaaS
model has its own caps, its own buckets (``ip:*``, ``tenant:*:*``),
and its own GC cadence. Keeping them siblings avoids the cross-module
dependency and lets each evolve independently.

The counter is deliberately simple: single row per bucket updated via
ORM write. Under concurrent contention two writers may both read the
same count and both increment — the actual throughput may exceed the
configured cap by a small factor. For our use case (preventing brute
force and cost DoS) that is acceptable; hardening to a strict atomic
cap would require ``SELECT ... FOR UPDATE`` or Redis.
"""
from datetime import timedelta

from odoo import api, fields, models, tools


# Defaults used when cloud.settings returns 0 or the singleton isn't
# ready yet (e.g. during an installation where the hook fires before
# the default record exists).
RATE_LIMIT_DEFAULTS = {
    'rate_limit_webhook_per_min': 300,
    'rate_limit_terminal_per_min': 30,
    'rate_limit_terminal_user_per_min': 10,
}


class CloudRateLimit(models.Model):
    _name = 'cloud.rate.limit'
    _description = 'Cloud Rate Limit Counter'

    bucket = fields.Char(required=True, index=True)
    window_start = fields.Datetime(required=True)
    count = fields.Integer(default=0)

    _bucket_uniq = models.Constraint(
        'unique (bucket)',
        'One row per rate-limit bucket.',
    )

    # ── Config accessors ────────────────────────────────────────────────────

    @api.model
    def _get_cap(self, key):
        """Return the configured cap for *key* or the documented default.

        Reads from the ``cloud.settings`` singleton. Any non-positive
        value falls back to the default so a misconfigured 0 can't
        accidentally lock every request out.
        """
        default = RATE_LIMIT_DEFAULTS[key]
        settings = self.env['cloud.settings'].sudo()._get()
        value = getattr(settings, key, 0) or 0
        return value if value > 0 else default

    # ── Hit / check ─────────────────────────────────────────────────────────

    @api.model
    def hit(self, bucket, cap_key=None,
            max_per_window=None, window_seconds=60):
        """Register a hit on *bucket*. Return True if allowed, False if over.

        Caller passes either ``cap_key`` (read from settings) or a
        literal ``max_per_window``. In test mode the check is bypassed
        unless the context flag ``force_rate_limit`` is set — tests
        that target this code opt in explicitly.
        """
        if tools.config.get('test_enable') and not (
            self.env.context.get('force_rate_limit')
        ):
            return True
        if max_per_window is None:
            if cap_key is None:
                raise ValueError(
                    "hit() requires cap_key or max_per_window",
                )
            max_per_window = self._get_cap(cap_key)
        now = fields.Datetime.now()
        rec = self.sudo().search([('bucket', '=', bucket)], limit=1)
        if not rec:
            self.sudo().create({
                'bucket': bucket,
                'window_start': now,
                'count': 1,
            })
            return True
        age = (now - rec.window_start).total_seconds()
        if age >= window_seconds:
            rec.sudo().write({'window_start': now, 'count': 1})
            return True
        if rec.count >= max_per_window:
            return False
        rec.sudo().write({'count': rec.count + 1})
        return True

    @api.model
    def _gc(self, older_than_hours=24):
        """Remove rows whose window is older than N hours. Cron-invoked."""
        cutoff = fields.Datetime.now() - timedelta(hours=older_than_hours)
        self.sudo().search([('window_start', '<', cutoff)]).unlink()
