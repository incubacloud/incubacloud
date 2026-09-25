"""Post-migrate for 1.0.130 — start every existing instance's clock at now.

1.0.130 adds ``last_touched_at``, the field the staging autopurge will
measure disuse against. Every row that predates this release arrives
with it empty, and there is no honest historical value to put there: the
panel signal it records — a person's own job finishing, a connect-as, a
terminal — was never captured before today.

The wrong answer is ``create_date``. It is available, it looks like a
reasonable proxy, and it would make the first tick after the deploy find
every staging older than the window already past its deadline. The
feature would open by doing the one thing it exists to promise never to
do: delete something without warning anyone. ``now`` instead gives every
existing instance the full window, which costs one window of patience
once and keeps the promise from the first day.

Production rows are stamped too. ``environment`` is editable, so a hole
there would turn into a staging with no clock the moment somebody
flipped one over.

It also enrols this release's two new crons on the platform bot.
``_incubacloud_assign_cron_user_id`` runs from the post-init hook, which
only fires on **install**, so a cron shipped in a later version lands on
an existing database still owned by uid 1.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Backfill ``last_touched_at``, then put the new crons on the bot.

    Only rows that have no clock are touched, so a re-run — or an
    upgrade that reaches this twice — cannot push a clock forward and
    hand an instance a second full window.
    """
    cr.execute(
        """
        UPDATE cloud_instance
           SET last_touched_at = (now() at time zone 'UTC')
         WHERE last_touched_at IS NULL
        """,
    )
    _logger.info(
        "1.0.130: started the activity clock on %s instance(s)", cr.rowcount,
    )
    # Idempotent: it only rewrites crons still pointing at uid 1, so one
    # an operator deliberately re-routed keeps their choice.
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
