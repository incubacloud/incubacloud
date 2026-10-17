"""Post-migrate for 1.0.188 — release instances a failed deploy left 'deploying'.

Until 1.0.188 a deploy that ended before its steps — the addon pre-flight
refusing, a TLS check, a host that would not connect — never reached
``on_failure``, the hook that puts the instance back in 'draft'. The
instance stayed in 'deploying' with no job running: it could be neither
deployed again nor deleted (aitrax, 16-oct-2026). A deploy blocked at
enqueue on a dependency conflict also waited in 'deploying'; it now waits
in 'draft' and re-enters 'deploying' when it is dispatched.

Every 'deploying' instance with no job in flight goes back to 'draft'.
Those whose deploy waits on an alert keep their status; the others get
'error', which is what ``on_failure`` would have written.

The cron bot also stops being notified by email: it has no address, so
every message it followed (queue_job subscribed it to each failed job)
ended as a failed mail notification.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_ACTIVE = ("pending", "enqueued", "wait_dependencies", "started")


def migrate(cr, version):
    """Move stranded 'deploying' instances to 'draft'; the bot reads its inbox."""
    cr.execute(
        """
        UPDATE cloud_instance i
           SET state = 'draft',
               status = CASE WHEN EXISTS (
                            SELECT 1 FROM cloud_job w
                             WHERE w.instance_id = i.id
                               AND w.blocked_alert_id IS NOT NULL)
                        THEN i.status ELSE 'error' END,
               write_date = (now() at time zone 'UTC')
         WHERE i.state = 'deploying'
           AND NOT EXISTS (
                SELECT 1 FROM cloud_job c
                 WHERE c.instance_id = i.id
                   AND c.blocked_alert_id IS NULL
                   AND c.state IN %s)
        RETURNING i.id
        """,
        (_ACTIVE,),
    )
    released = [row[0] for row in cr.fetchall()]
    if released:
        _logger.info(
            "1.0.188: %d instance(s) left 'deploying' with no job in flight "
            "moved back to 'draft': %s", len(released), released,
        )
    # Through the ORM: Odoo derives ``notification_type`` from a group,
    # which the bot's group alignment now includes.
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.users']._incubacloud_ensure_cron_bot()
