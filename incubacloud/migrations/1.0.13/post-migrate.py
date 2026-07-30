"""Post-migrate for 1.0.13.

Finish the lifecycle split started in ``pre-migrate.py``:

1. ``status`` is health only now, so the old ``provisioning`` value has
   no home. Those rows were staging clones waiting for their deploy
   chain; they land in 'ok' with whatever ``state`` the pre-migrate
   derived. A clone that never deployed is a draft, which is
   re-deployable, and a deploy job that is still running will call
   ``_transition('deployed')`` on success — legal from 'draft'.
2. Re-derive ``deployed`` from ``state`` in SQL rather than trusting the
   ORM to recompute a field that just changed from stored-plain to
   stored-computed.

Idempotent: both statements are pure functions of the current rows, so
running them twice yields the same result.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Drop the 'provisioning' status and re-derive ``deployed``."""
    cr.execute(
        "UPDATE cloud_instance SET status = 'ok' WHERE status = 'provisioning'"
    )
    if cr.rowcount:
        _logger.info(
            "incubacloud 1.0.13: moved %s instance(s) off the removed "
            "'provisioning' status",
            cr.rowcount,
        )

    # Mirrors cloud.instance._ON_HOST_STATES.
    cr.execute(
        """
        UPDATE cloud_instance
           SET deployed = (state IN ('deployed', 'deleting'))
         WHERE deployed IS DISTINCT FROM (state IN ('deployed', 'deleting'))
        """
    )
    if cr.rowcount:
        _logger.info(
            "incubacloud 1.0.13: re-derived 'deployed' for %s instance(s)",
            cr.rowcount,
        )
