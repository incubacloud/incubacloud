"""The direct entrypoint: a port of its own for the platform's own traffic.

Some of what reaches a host is the platform talking to itself: agents
pushing metrics, panels calling their manager. Through the CDN that is a
detour and a dependency, and a host behind the CDN refuses everything
else on 443. So a host can open a second Traefik entrypoint, ``direct``,
on a port of its own (``direct_port``), admitting only the platform's
addresses -- in nftables and in Traefik -- with a certificate from Let's
Encrypt obtained by DNS-01 (plan-trafico-interno-directo, 27-oct-2026).

This file holds what the host knows about it: the port and what was
applied, the list of admitted addresses, the documents the setup jobs
ship, and which URL each host's agents write to. The job that applies the
list and probes the names is ``sync_direct_path``.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

#: Marks every line this file adds to a host's ``traefik.yml`` and proxy
#: compose file, so a render removes exactly what the last one added.
DIRECT_MARK = "# incubacloud:direct"

#: Ports the platform publishes on a host, whatever the host says: mail,
#: web, Traefik's dashboard and metrics, the central's gateway and Grafana.
_PLATFORM_PORTS = frozenset({25, 80, 143, 443, 465, 587, 993, 3000, 8080, 8082, 8428})


class CloudHost(models.Model):
    _inherit = "cloud.host"

    direct_port = fields.Integer(
        string="Direct port",
        default=0,
        help="Port the platform's own servers use to reach this host "
             "without the CDN. Applied by Full Setup.",
    )
    direct_port_shipped = fields.Integer(
        string="Direct port applied",
        readonly=True,
        copy=False,
        help="The direct port the last Full Setup published on the proxy. "
             "0: none.",
    )
    direct_allowlist_shipped = fields.Text(
        string="Admitted on the direct port",
        readonly=True,
        copy=False,
        help="Addresses the host admits on its direct port, in nftables and "
             "in Traefik, one per line, as the last good sync_direct_path "
             "applied them.",
    )
    direct_allowlist_synced_at = fields.Datetime(
        string="Direct list applied at", readonly=True, copy=False,
    )
    direct_allowlist_stale_since = fields.Datetime(
        string="Direct list behind since", readonly=True, copy=False,
    )
    direct_path_opt_out = fields.Boolean(
        string="Use the public path",
        help="This host's agents, and the panels that run on it, keep going "
             "through the CDN.",
    )
    metrics_agents_write_url = fields.Char(
        string="Agents write to",
        readonly=True,
        copy=False,
        help="The URL this host's agents were installed with. Empty: "
             "installed before the direct path, with the remote-write URL "
             "in Settings.",
    )
    direct_reach_ok = fields.Boolean(
        string="Direct path reached from the host", readonly=True, copy=False,
    )
    direct_reach_urls = fields.Text(
        string="Direct URLs that answered", readonly=True, copy=False,
        help="The direct URLs this host's last agents install reached, one "
             "per line.",
    )
    direct_reach_at = fields.Datetime(
        string="Direct path last tried", readonly=True, copy=False,
    )
    direct_reach_failures = fields.Integer(
        string="Direct path failures in a row", readonly=True, copy=False,
    )
    direct_refused_at = fields.Datetime(
        string="Direct path last refused", readonly=True, copy=False,
    )
    direct_reach_reason = fields.Char(
        string="Why the direct path is not used", readonly=True, copy=False,
    )

    @api.constrains("direct_port", "port")
    def _check_direct_port(self):
        """Refuse a direct port the platform already uses on this host.

        A port taken by another published service makes the proxy's
        ``up -d`` fail, and the proxy serves the whole host. The SSH port
        counts, and so does the range the hardening picks it from: SSH
        may rotate onto that number later.

        :raise ValidationError: naming the port.
        """
        # Imported here: the executor module pulls in the whole executor
        # stack, which this model does not otherwise need at import time.
        from .host_hardening_executor import _SSH_PORT_MAX, _SSH_PORT_MIN

        for host in self.sudo():
            port = host.direct_port or 0
            if not port:
                continue
            if (
                port < 1024 or port > 65535
                or port in _PLATFORM_PORTS
                or port == host.port
                or _SSH_PORT_MIN <= port <= _SSH_PORT_MAX
            ):
                raise ValidationError(_(
                    "Port %(port)s is used by the platform on this host; "
                    "choose another (for example 9443).",
                    port=port,
                ))

    def _render_config_snapshot(self):
        """Add the direct port to the snapshot, only when the host has one.

        Every key in the snapshot is part of every host's digest, so a key
        rendered as ``""`` for hosts without a port would mark the whole
        fleet "configuration pending". Absent, a host without a port keeps
        the digest it had.

        :rtype: dict
        """
        snap = super()._render_config_snapshot()
        port = self.sudo().direct_port
        if port:
            snap["direct_port"] = port
        return snap
