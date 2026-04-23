"""Post-migrate for 1.0.3 — drop orphan cloud.job.type records.

``setup_host`` and ``setup_traefik`` were never enqueued from any UI
or code path — their logic was inlined into ``full_setup`` before the
product shipped. The declarative records lingered in every install as
dead dropdown entries; this migration removes them safely.

Guarded against the (hypothetical) case where some fork enqueued one
of these types directly: if any cloud.job still references the type,
the unlink is skipped for that type and a warning is logged.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

_OBSOLETE = ('setup_host', 'setup_traefik')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    JobType = env['cloud.job.type'].sudo()
    Job = env['cloud.job'].sudo()
    for code in _OBSOLETE:
        types = JobType.search([('code', '=', code)])
        if not types:
            continue
        used = Job.search_count([('job_type_id', 'in', types.ids)])
        if used:
            _logger.warning(
                "Skipping unlink of cloud.job.type '%s': %s cloud.job "
                "row(s) still reference it.", code, used,
            )
            continue
        types.unlink()
        _logger.info("Dropped obsolete cloud.job.type '%s'.", code)
