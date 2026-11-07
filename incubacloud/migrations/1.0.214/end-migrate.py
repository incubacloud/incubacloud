"""End-migrate for 1.0.214 — the edge guard on every host that is already set up.

From this version every host's proxy refuses Odoo's ``/website/info``
(``cloud_host_edge_guard.py``; owner's decision, 7-nov-2026). A host set up
from now on gets the guard from its Full Setup; one set up before gets it
from ``push_edge_guard``, queued here once per active host with the
panel's proxy. Never a Full Setup.

``end`` and not ``post``: Odoo runs a module's ``post-migrate`` before the
modules that depend on it are loaded, so a module that leaves a host alone
(``_edge_guard_leaves_alone``, the saas master's own host) would not have
been asked yet. The ``end-*`` scripts run with every module loaded
(``odoo/modules/loading.py``).

Idempotent: the job writes the file only when it differs, and a waiting
one collapses the next.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Queue the edge guard on every host that should carry it.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["cloud.host"]._edge_guard_everywhere("upgrade 1.0.214")
