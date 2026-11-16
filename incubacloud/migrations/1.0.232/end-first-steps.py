"""End-migrate for 1.0.232 — the first steps' marks of a panel already running.

From this version the panel keeps when each first step was first tried and
first done (``cloud.first.step``). A panel updated to it gets the marks of
what it already holds, marked ``backfilled``: the dates are those of the
records, not of a moment seen (``cloud.first.steps._backfill``). The
closing of the card by each user is left as it is: a panel whose first
deploy is backfilled does not see the card appear for its optional steps.

``end`` and not ``post``: a layer that turns the first steps off
(``_enabled``) is loaded only once every module is, and then nothing is
written.

Idempotent: a date written is never changed. It never breaks the update:
each mark is written in a savepoint of its own (``_reconcile``,
``_backfill``), and whatever still fails is logged and leaves the marks
for the panel's next reads to fill.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Write the marks of what the panel already holds.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        with cr.savepoint():
            env["cloud.first.steps"]._backfill()
    except Exception:  # the update goes on without the marks
        _logger.exception("First steps: backfilling the marks failed")
