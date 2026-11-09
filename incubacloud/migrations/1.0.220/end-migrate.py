"""End-migrate for 1.0.220 — the edge guard and a Check where older code left hosts short.

Two independent repairs, each in its own function:

**The edge guard on the hosts 1.0.214 left without it**
(``_queue_edge_guard``). The update to 1.0.214 queued ``push_edge_guard``
on every host already set up. On a panel rebuilt by another panel the
update runs in a container of the new image while the old server keeps
serving, and that server's runner took the job at once with code that had
no executor for it: the job failed (``No executor registered``), nothing
queued it again, and the host stayed without the guard (a tenant's panel,
7-nov-2026). From 1.0.220 such a job waits for a server with newer code
(``cloud.job._executor_or_postpone``); this queues the guard again on
every host that should carry it and has never had it written
(``edge_guard_shipped_hash`` empty). The server that takes it now knows
the type. Its success resolves the ``job_failed`` alert the first one left
(``cloud.job._dismiss_job_failed_alerts``).

**A Check again on the hosts the old one left Unsupported**
(``_queue_checks``). Until this version a Check (``host_probe``) that
found under 10 GB free failed its job, raised ``job_failed`` and marked
the host ``unsupported``, though its instances ran on. From 1.0.220 the
Check gives that as a verdict (``degraded`` with ``host_low_disk``), but a
host it left so before the update stays there until someone runs a Check:
one is queued here on each active host that is ``unsupported`` with the
failure of a Check still open. Its success closes that ``job_failed``
(``cloud.job._dismiss_job_failed_alerts``) and gives the host its status,
whatever its disk now holds. A Check that cannot be queued is logged and
leaves the update going: the host stays as it was, and a Check by hand
settles it.

``end`` and not ``post``, as 1.0.214's: ``enqueue`` runs with every module
loaded, so a layer that has its say on the queue (or that leaves a host
alone, ``_edge_guard_leaves_alone``) is asked.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Queue the edge guard and a Check where older code left hosts short.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    _queue_edge_guard(env)
    _queue_checks(env)


def _queue_edge_guard(env):
    """Queue the edge guard on the hosts that should carry it and never got it.

    :param env: superuser environment
    """
    hosts = env["cloud.host"]._edge_guard_targets().filtered(
        lambda host: not host.edge_guard_shipped_hash,
    )
    queued = hosts.browse()
    for host in hosts:
        if host._enqueue_edge_guard("upgrade 1.0.220"):
            queued |= host
    _logger.info(
        "[edge-guard] queued again on %d host(s) without it: %s",
        len(queued), queued.ids,
    )


def _queue_checks(env):
    """Queue a Check on every active host a failed Check left unsupported.

    :param env: superuser environment
    """
    hosts = env["cloud.alert"].search([
        ("code", "=", "job_failed"),
        ("state", "=", "active"),
        ("dedup_key", "=", "job_type:host_probe"),
        ("instance_id", "=", False),
        ("host_id.active", "=", True),
        ("host_id.status", "=", "unsupported"),
    ]).host_id
    queued = []
    for host in hosts:
        try:
            with env.cr.savepoint():
                env["cloud.job"].enqueue(host.id, False, "host_probe")
            queued.append(host.id)
        except Exception as exc:  # noqa: BLE001 — never fail the update
            _logger.warning(
                "[upgrade 1.0.220] could not queue a Check on host %s: %s",
                host.id, exc,
            )
    _logger.info(
        "[upgrade 1.0.220] Check queued on %d host(s) left unsupported: %s",
        len(queued), queued,
    )
