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
import ipaddress
import logging
from datetime import timedelta

import yaml

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

#: Marks every line this file adds to a host's ``traefik.yml`` and proxy
#: compose file, so a render removes exactly what the last one added.
DIRECT_MARK = "# incubacloud:direct"

#: Ports the platform publishes on a host, whatever the host says: mail,
#: web, Traefik's dashboard and metrics, the central's gateway and Grafana.
_PLATFORM_PORTS = frozenset({25, 80, 143, 443, 465, 587, 993, 3000, 8080, 8082, 8428})

#: Traffic born on the host itself reaches its own direct port with a
#: container's address (Docker's DNAT and masquerade), never its public one.
DIRECT_PRIVATE_RANGES = ("10.0.0.0/8", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

#: Where the job writes the dynamic document Traefik reads the list from.
DIRECT_DYNAMIC_FILE = "~/traefik/dynamic/direct.yml"

#: A host absent from the metrics for this long may not be ours any more:
#: Hetzner hands a released address to someone else within hours.
_ABSENT_FOR = timedelta(hours=24)


def global_ipv4(value):
    """Return *value* as a global IPv4 address, or ''.

    Only such an address is the one a host's traffic leaves with. A name,
    an IPv6 address, ``0.0.0.0`` (a host bought and not answered yet), a
    private, shared or documentation address all say nothing about it.

    :param value: an address as a host or a reservation stores it.
    :rtype: str
    """
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return ""
    if address.version != 4 or not address.is_global:
        return ""
    return str(address)


def sorted_networks(entries):
    """Return *entries* without duplicates, in address order.

    :param entries: CIDR strings.
    :rtype: list
    """
    return sorted(set(entries), key=ipaddress.ip_network)


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

    @api.model
    def _direct_absent_hosts(self, hosts):
        """Return the hosts of *hosts* missing from the metrics for over a day.

        Read from the ``metrics_host_absent`` alert, open and older than
        ``_ABSENT_FOR``. When it is resolved the host comes back at the
        next pass of the job.

        :param hosts: ``cloud.host`` records.
        :rtype: cloud.host
        """
        alerts = self.env["cloud.alert"].sudo().search([
            ("code", "=", "metrics_host_absent"),
            ("state", "=", "active"),
            ("host_id", "in", hosts.ids),
            ("create_date", "<", fields.Datetime.now() - _ABSENT_FOR),
        ])
        return alerts.host_id

    def _direct_excluded(self):
        """Whether this active host's address must stay out of the list.

        Core knows no such case beyond the absence above; the SaaS layer
        leaves out a host bought on demand whose server is gone.

        :rtype: bool
        """
        self.ensure_one()
        return False

    @api.model
    def _direct_hosts_entries(self):
        """Return ``(entries, left_out)`` for ``direct-hosts``.

        The private ranges and the /32 of every active host with a global
        IPv4 address, without duplicates and in order; *left_out* names
        the hosts kept out although active, for the job's log.

        :rtype: tuple
        """
        hosts = self.sudo().search([("active", "=", True)])
        absent = self._direct_absent_hosts(hosts)
        entries, left_out = list(DIRECT_PRIVATE_RANGES), []
        for host in hosts:
            address = global_ipv4(host.ip_address)
            if not address:
                continue
            if host in absent or host._direct_excluded():
                left_out.append(host.name)
                continue
            entries.append(f"{address}/32")
        return sorted_networks(entries), sorted(left_out)

    @api.model
    def _direct_extra_peers(self):
        """Return what a layer above admits on the direct port besides hosts.

        Core admits nothing more; the SaaS layer adds the servers its
        customers rent while their reservation lives.

        :return: CIDR strings.
        :rtype: list
        """
        return []

    @api.model
    def _direct_peers_entries(self):
        """Return the whole list the direct port admits (``direct-peers``).

        :rtype: list
        """
        hosts, _left_out = self._direct_hosts_entries()
        return sorted_networks(hosts + list(self._direct_extra_peers()))

    def _direct_admits(self, address):
        """Whether this host has *address* applied, by its own /32.

        Never by a range it falls in: a private address is admitted only
        to what was born on this host, and says nothing about another.

        :param str address: an address as a host or reservation stores it.
        :rtype: bool
        """
        self.ensure_one()
        ip = global_ipv4(address)
        if not ip:
            return False
        return f"{ip}/32" in (self.sudo().direct_allowlist_shipped or "").split()

    @api.model
    def _shipped_direct_yml(self):
        """Return the dynamic document every direct entrypoint reads.

        The two lists, the header stripper, the per-source rate limit and
        the router that answers 403 to whatever has no router of its own
        above priority 10000 (Traefik 2.11 attaches a router without
        entry points to every one of them). The TLS option ``direct``
        asks no client certificate; only the two routers allowed on the
        port name it.

        :rtype: str
        """
        hosts, _left_out = self._direct_hosts_entries()
        document = {
            "http": {
                "middlewares": {
                    "direct-peers": {
                        "ipWhiteList": {"sourceRange": self._direct_peers_entries()},
                    },
                    "direct-hosts": {"ipWhiteList": {"sourceRange": hosts}},
                    "direct-strip": {"headers": {"customRequestHeaders": {
                        "CF-Connecting-IP": "",
                        "CF-Connecting-IPv6": "",
                        "True-Client-IP": "",
                    }}},
                    "direct-ratelimit": {
                        "rateLimit": {"average": 600, "period": "1m", "burst": 300},
                    },
                    "direct-deny-all": {
                        "ipWhiteList": {"sourceRange": ["127.0.0.2/32"]},
                    },
                },
                "routers": {
                    "direct-deny": {
                        "rule": "PathPrefix(`/`)",
                        "priority": 10000,
                        "entryPoints": ["direct"],
                        "middlewares": ["direct-deny-all"],
                        "service": "noop@internal",
                        "tls": {},
                    },
                },
            },
            "tls": {"options": {"direct": {"minVersion": "VersionTLS12"}}},
        }
        body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
        return "# Auto-generated by IncubaCloud. Do not edit by hand.\n" + body
