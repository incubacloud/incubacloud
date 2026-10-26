"""Post-migrate for 1.0.198 — the job queue per instance.

``cloud.job.state`` is the job's own field now, with ``queued`` for a
job waiting in its target's queue. Two things of the old shape are
settled here:

* A job whose queue.job was deleted before 1.0.90 cut the link first
  has an empty state (29 in production, July–August 2026): not waiting
  for an alert, with no queue.job, it is nothing the queue can serve.
  It ends ``cancelled``, with a note saying why.
* The pushes that arrived during a rebuild were kept in
  ``cloud.instance.pending.push`` and folded into one rebuild when it
  ended. A push now merges into the rebuild waiting in the instance's
  queue, so the model and its table go. Nothing is migrated from it: a
  row there described a rebuild that the next push or rebuild would
  have folded in anyway.

The sweep cron of the queues (``cron_job_queue_dispatch``) arrives with
this version; on an existing database it lands owned by uid 1, so it is
put on the platform bot like every other cron of the module.

**The doodba template pin.** 20.0 is offered from this version, and a
panel offers it only if its pinned template can render it (v10.0.3, the
floor on 20.0's card). The pin is a setting of each panel, and a field's
new default never reached a panel installed before it: a panel installed
with a core older than 1.0.195 (21-oct-2026) holds v9.6.1, unless someone
changed it. A version tag below the floor is raised to v10.0.8, the
field's default since 1.0.195; an empty pin, a branch or a commit means
something chosen and is left. Each stack moves to the new template at its
next deploy or rebuild, as when the setting is changed by hand (RB-15).
"""
import logging
import re

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_NOTE = "Cancelled: no queue.job when the job queue was introduced."

# Frozen with this migration, not read from the code it upgrades to: 20.0's
# floor (``template_min`` on its card) and the field's default as 1.0.198
# ships them. A panel may upgrade from 1.0.197 or older long after the card
# of 20.0 is retired or the default moves; reading them then would fail with
# a KeyError, or raise the pin to a template nobody tested with 1.0.198.
_FLOOR = "v10.0.3"
_RAISED_TO = "v10.0.8"
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def migrate(cr, version):
    """Settle the stateless jobs, drop the pending-push table, enrol the
    cron and raise a template pin too old for 20.0."""
    cr.execute(
        """
        UPDATE cloud_job
           SET state = 'cancelled',
               write_date = (now() at time zone 'UTC')
         WHERE state IS NULL
           AND queue_job_id IS NULL
           AND blocked_alert_id IS NULL
        RETURNING id
        """
    )
    job_ids = [row[0] for row in cr.fetchall()]
    _logger.info(
        "cloud.job: %d job(s) with no queue.job and no state cancelled",
        len(job_ids),
    )
    if job_ids:
        cr.execute(
            """
            INSERT INTO cloud_job_log_chunk
                (job_id, content, source, create_date, write_date)
            SELECT id, %s, 'system',
                   (now() at time zone 'UTC'), (now() at time zone 'UTC')
              FROM cloud_job
             WHERE id IN %s
            """,
            (_NOTE, tuple(job_ids)),
        )
    cr.execute("DROP TABLE IF EXISTS cloud_instance_pending_push CASCADE")
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud",
    )
    _raise_the_template_pin(env)


def _tag(ref):
    """Return the version of a template tag as a tuple of ints.

    :param str ref: a pin, ``vX.Y.Z`` or ``X.Y.Z``.
    :return: ``(X, Y, Z)``, or None when *ref* is no version tag (empty,
        a branch or a commit).
    :rtype: tuple
    """
    match = _TAG_RE.match((ref or "").strip())
    return tuple(map(int, match.groups())) if match else None


def _raise_the_template_pin(env):
    """Raise the panel's template pin to ``_RAISED_TO`` if 20.0 cannot use it.

    Only a version tag older than ``_FLOOR`` moves; an empty pin (the
    template's default branch), a branch or a commit stay, as does a tag
    at or past the floor. Logs the old and the new pin when it moves, and
    nothing when it stays.

    :param env: an environment of the superuser.
    """
    settings = env["cloud.settings"].search([], limit=1)
    if not settings:
        return
    pinned = settings.copier_template_ref or ""
    tag = _tag(pinned)
    if tag is None or tag >= _tag(_FLOOR):
        return
    settings.copier_template_ref = _RAISED_TO
    _logger.info(
        "cloud.settings: doodba template pin %s raised to %s "
        "(Odoo 20.0 needs %s or newer)",
        pinned, _RAISED_TO, _FLOOR,
    )
