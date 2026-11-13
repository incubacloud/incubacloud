"""The guard on the proxy's shared network: instances do not reach each other.

Every instance's ``odoo`` container joins ``inverseproxy_shared``, the
network the host's Traefik reaches it on, and Docker lets any two
containers of one bridge network talk. So code running in one instance
could open the 8069 and 8072 of every other instance awake on the host,
past the CDN, the edge routers and the per-address limits (measured on
13-nov-2026). Docker's ``icc`` setting does not reach a compose network,
and the network's own ``enable_icc`` can only be set by recreating it,
which stops every instance of the host.

The guard is a chain of the host's firewall, ``IC-SHARED``, jumped to
from Docker's ``DOCKER-USER`` for packets that enter and leave the
network's bridge: replies of established connections pass, and so do
packets from the proxy's fixed address (``PROXY_SHARED_MAC``); anything
else is dropped. ``scripts/shared_guard.sh`` writes it, and a systemd
timer applies it again every minute, because the rules are lost at
every reboot. It fails open: while the running proxy has another
address, the chain is removed, or the host would stop answering.

Full Setup runs the step after the proxy starts, and turns it off on a
host where ``shared_guard`` is off (RB-39).
"""
from odoo import fields, models

#: The Full Setup step that installs, or removes, the guard.
SHARED_GUARD_STEP = "Isolate the instances on the proxy network"

#: The alert of a host whose guard Full Setup left open.
SHARED_GUARD_ALERT = "shared_guard_open"

#: The marker each run of the script ends with: ``<prefix><STATE> <reason>``.
SHARED_GUARD_MARKER = "IC_SHARED_GUARD_"

#: The states that leave the host as asked: guarded, or the guard removed.
SHARED_GUARD_SETTLED = ("ON", "OFF")


def shared_guard_verdict(result):
    """Return what the guard step says it left on the host.

    The script ends with one marker line (``IC_SHARED_GUARD_ON``,
    ``_OFF``, ``_OPEN <reason>`` or ``_UNSUPPORTED <reason>``); the last
    one counts. A run that exited non-zero, or printed none, is ``FAILED``.

    :param dict result: the step's result (``stdout``, ``exit_status``)
    :return: ``(state, reason)``; the reason may be empty
    :rtype: tuple
    """
    stdout = (result or {}).get("stdout") or ""
    exit_status = (result or {}).get("exit_status", 1)
    marker = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(SHARED_GUARD_MARKER):
            marker = line[len(SHARED_GUARD_MARKER):]
    if exit_status != 0 or not marker:
        return "FAILED", f"exit {exit_status}"
    state, _sep, reason = marker.partition(" ")
    return state, reason.strip()


class CloudHostSharedGuard(models.Model):
    _inherit = "cloud.host"

    shared_guard = fields.Boolean(
        string="Isolate Instances on the Proxy Network",
        default=True,
        help="Only the host's proxy may open connections to the instances "
             "on the network they share with it, so one instance cannot "
             "reach another one's Odoo directly. Applied, or removed, by "
             "the next Full Setup. Turn it off only if the guard gets in "
             "the way; see RB-39.",
    )
