"""What each user's conversations cost, per month and model.

Kept apart from the conversations: deleting a conversation, or its
expiry, must not take its cost out of the month (it would undo the
monthly limit). One row per user, month, provider and model, added to in
one statement.
"""

from odoo import api, fields, models


class AssistantUsage(models.Model):
    """Tokens and approximate cost of one user's calls of one model in one month."""

    _name = "incubacloud.assistant.usage"
    _description = "Assistant usage per user and month"
    _order = "month desc, user_id"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade", readonly=True
    )
    month = fields.Date(required=True, index=True, readonly=True, help="Its first day.")
    provider = fields.Char(required=True, readonly=True)
    model = fields.Char(required=True, readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    cache_read_tokens = fields.Integer(readonly=True)
    cache_write_tokens = fields.Integer(readonly=True)
    cost = fields.Float(digits=(16, 6), readonly=True, help="Euros, approximately.")
    unpriced_tokens = fields.Integer(
        readonly=True, help="Tokens of calls whose price is not known."
    )

    _user_month_model = models.Constraint(
        "UNIQUE(user_id, month, provider, model)",
        "One row per user, month and model.",
    )

    @api.model
    def _add(self, user_id, provider, model, usage, cost):
        """Add one call's tokens and cost to its month's row.

        :param usage: a ``connectors.base.Usage``
        :param cost: euros, or None when the model's price is not known
        """
        month = fields.Date.today().replace(day=1)
        total = (
            usage.input_tokens
            + usage.output_tokens
            + usage.cache_read_tokens
            + usage.cache_write_tokens
        )
        self.env.cr.execute(
            """
            INSERT INTO incubacloud_assistant_usage
                (user_id, month, provider, model, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, cost, unpriced_tokens,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    now() at time zone 'UTC', now() at time zone 'UTC')
            ON CONFLICT (user_id, month, provider, model) DO UPDATE SET
                input_tokens = incubacloud_assistant_usage.input_tokens
                    + EXCLUDED.input_tokens,
                output_tokens = incubacloud_assistant_usage.output_tokens
                    + EXCLUDED.output_tokens,
                cache_read_tokens = incubacloud_assistant_usage.cache_read_tokens
                    + EXCLUDED.cache_read_tokens,
                cache_write_tokens = incubacloud_assistant_usage.cache_write_tokens
                    + EXCLUDED.cache_write_tokens,
                cost = incubacloud_assistant_usage.cost + EXCLUDED.cost,
                unpriced_tokens = incubacloud_assistant_usage.unpriced_tokens
                    + EXCLUDED.unpriced_tokens,
                write_date = EXCLUDED.write_date
            """,
            (
                user_id,
                month,
                provider,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                cost or 0.0,
                0 if cost is not None else total,
                self.env.uid,
                self.env.uid,
            ),
        )
        self.invalidate_model()

    @api.model
    def _month_cost(self, user_id):
        """Return ``(euros this month, whether some tokens have no known price)``."""
        self.env.cr.execute(
            "SELECT COALESCE(SUM(cost), 0), COALESCE(SUM(unpriced_tokens), 0) "
            "FROM incubacloud_assistant_usage WHERE user_id = %s AND month = %s",
            (user_id, fields.Date.today().replace(day=1)),
        )
        cost, unpriced = self.env.cr.fetchone()
        return float(cost), bool(unpriced)

    @api.model
    def _usage_table(self, months=12):
        """Return the usage of every user, per month, newest first (managers)."""
        first = fields.Date.subtract(
            fields.Date.today().replace(day=1), months=months - 1
        )
        self.env.cr.execute(
            """
            SELECT u.user_id, p.name, u.month, SUM(u.input_tokens),
                   SUM(u.output_tokens), SUM(u.cache_read_tokens),
                   SUM(u.cache_write_tokens), SUM(u.cost), SUM(u.unpriced_tokens)
              FROM incubacloud_assistant_usage u
              JOIN res_users r ON r.id = u.user_id
              JOIN res_partner p ON p.id = r.partner_id
             WHERE u.month >= %s
          GROUP BY u.user_id, p.name, u.month
          ORDER BY u.month DESC, p.name
            """,
            (first,),
        )
        return [
            {
                "user_id": row[0],
                "user_name": row[1],
                "month": row[2].strftime("%Y-%m"),
                "input_tokens": row[3],
                "output_tokens": row[4],
                "cache_read_tokens": row[5],
                "cache_write_tokens": row[6],
                "cost": round(float(row[7]), 4),
                "unpriced_tokens": row[8],
            }
            for row in self.env.cr.fetchall()
        ]
