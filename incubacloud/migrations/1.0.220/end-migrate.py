"""End-migrate for 1.0.220 — the edge guard on the hosts 1.0.214 left without it.

The update to 1.0.214 queued ``push_edge_guard`` on every host already set
up. On a panel rebuilt by another panel the update runs in a container of
the new image while the old server keeps serving, and that server's runner
took the job at once with code that had no executor for it: the job failed
(``No executor registered``), nothing queued it again, and the host stayed
without the guard (a tenant's panel, 7-nov-2026). From 1.0.220 such a job
waits for a server with newer code (``cloud.job._executor_or_postpone``);
this queues the guard again on every host that should carry it and has
never had it written (``edge_guard_shipped_hash`` empty). The server that
takes it now knows the type. Its success resolves the ``job_failed`` alert
the first one left (``cloud.job._dismiss_job_failed_alerts``).

``end`` for the reason 1.0.214 gives: with every module loaded, a layer
that leaves a host alone (``_edge_guard_leaves_alone``) has been asked.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Queue the edge guard on the hosts that should carry it and never got it.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
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
