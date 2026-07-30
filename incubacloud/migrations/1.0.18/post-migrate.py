"""Post-migrate for 1.0.18 — two accumulated-growth cleanups.

Part 1 — background-job audit noise.

``cloud.job.enqueue`` used to write a ``cloud.audit.log`` row for every
job, including the cron-driven background types (``host_metrics``,
``instance_health``, ``docker_prune``). Those fire per host and per
instance on every tick, so on any database that has been running for a
while they outnumber real operator actions by orders of magnitude — the
audit trail is unreadable and the table grows without bound.

Enqueue now filters them out. This clears the history they already
wrote, so the audit page becomes usable immediately instead of after a
full retention window.

Deliberately a raw DELETE joined on the job type: the ORM would load
millions of rows to unlink them, and ``cloud.audit.log`` is append-only
by ACL so there is no unlink path to respect anyway.

Part 2 — GitHub events that never had a handler.

The webhook controller only ever marked ``push`` and ``pull_request``
rows as processed. Everything else — ``installation``, and the
``create``/``delete`` types the App manifest subscribes to — stayed
``processed=False`` forever, and both retention stages filter on
``processed=True``. The rows and their full payloads were therefore
exempt from a retention policy that looked like it covered them.

The controller now marks them. This flags the backlog so the existing
cron can reap it. Rows carrying an ``error`` are left alone: those are
genuine processing failures and the retention design deliberately keeps
them for investigation.
"""
import logging

_logger = logging.getLogger(__name__)

# Mirrors ``cloud.job._hidden_job_types``. Hard-coded rather than read
# from the registry: a migration must describe the data as it was at this
# version, not follow a list that later versions may extend.
_HIDDEN_JOB_TYPES = ('host_metrics', 'docker_prune', 'instance_health')


# Event types that had a handler setting ``processed`` before 1.0.18.
# Anything else was persisted and abandoned.
_HANDLED_EVENT_TYPES = ('push', 'pull_request')


def migrate(cr, version):
    """Clear background-job audit noise and release the GitHub event backlog."""
    cr.execute(
        """
        DELETE FROM cloud_audit_log a
        USING cloud_job j, cloud_job_type t
        WHERE a.job_id = j.id
          AND j.job_type_id = t.id
          AND t.code IN %s
        """,
        (_HIDDEN_JOB_TYPES,),
    )
    if cr.rowcount:
        _logger.info(
            "incubacloud 1.0.18: removed %d background-job audit row(s)",
            cr.rowcount,
        )

    cr.execute(
        """
        UPDATE cloud_github_event
        SET processed = TRUE
        WHERE processed IS NOT TRUE
          AND error IS NULL
          AND (event_type IS NULL OR event_type NOT IN %s)
        """,
        (_HANDLED_EVENT_TYPES,),
    )
    if cr.rowcount:
        _logger.info(
            "incubacloud 1.0.18: released %d unhandled GitHub event(s) "
            "to the retention cron",
            cr.rowcount,
        )
