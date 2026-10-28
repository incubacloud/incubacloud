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
import re
import shlex
from datetime import timedelta

import yaml

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import _direct_probe
from .cloud_host import strip_marked_blocks

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

#: How long the list in force may lag behind before it is said aloud.
_STALE_AFTER = timedelta(minutes=30)

#: A host that turned the direct path down is not offered it again for this long.
_REFUSED_FOR = timedelta(hours=6)

#: A host on the direct path whose metrics stopped this long is re-applied.
_SILENT_AFTER = timedelta(minutes=20)

#: And not more often than this.
_REAPPLY_EVERY = timedelta(hours=1)

#: How old the last good sync may be before the cron runs it anyway: it
#: puts back what a reboot or an old re-hardening took, and probes again.
_RESYNC_EVERY = timedelta(hours=1)


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


#: The DNS token's file on the host (``~/traefik/secrets``) and in the proxy.
DIRECT_TOKEN_NAME = "cf-dns-api-token"
DIRECT_TOKEN_IN_PROXY = f"/etc/traefik/secrets/{DIRECT_TOKEN_NAME}"

_LETSENCRYPT_RE = re.compile(
    r"^(?P<indent>[ \t]+)letsencrypt:[ \t]*\n"
    r"(?P<body>(?:(?P=indent)[ \t]+[^\n]*(?:\n|$)|[ \t]*\n)*)",
    re.MULTILINE,
)


def letsencrypt_block_span(text):
    """Return where the ``letsencrypt`` resolver's body is in *text*.

    :param str text: a ``traefik.yml``.
    :return: ``(start, end)`` offsets of the lines under the resolver's
        name, or None when there is no such resolver.
    :rtype: tuple | None
    """
    match = _LETSENCRYPT_RE.search(text or "")
    return (match.start("body"), match.end("body")) if match else None


def _letsencrypt_value(text, key):
    """Return *key*'s value in the ``letsencrypt`` resolver, or ''.

    A commented line (``#caServer: ...``, as every production host has
    it) is no value: the line has to start with the key.

    :param str text: a ``traefik.yml``.
    :param str key: ``email`` or ``caServer``.
    :rtype: str
    """
    span = letsencrypt_block_span(text)
    if not span:
        return ""
    body = text[span[0]:span[1]]
    match = re.search(
        rf'^[ \t]+{key}:[ \t]*"?([^"\n#]*?)"?[ \t]*$', body, re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def _direct_entrypoint_lines(port):
    """Return the ``direct`` entrypoint, one line each, without indentation.

    No ``forwardedHeaders``: Traefik then drops every ``X-Forwarded-*``
    and ``X-Real-Ip`` it is sent and writes the connecting address. No
    TLS options either: the two routers allowed on the port name theirs,
    and options on the entrypoint would clash with those of the routers
    that listen everywhere.

    :param int port: the direct port.
    :rtype: list
    """
    return [
        f"direct:  {DIRECT_MARK}",
        f'  address: ":{int(port)}"',
        "  http:",
        "    tls: {}",
        "    middlewares:",
        "      - direct-peers@file",
        "      - direct-strip@file",
        "      - direct-ratelimit@file",
        "  transport:",
        "    respondingTimeouts:",
        "      readTimeout: 30s",
        "      writeTimeout: 60s",
        "      idleTimeout: 90s",
        "  http2:",
        "    maxConcurrentStreams: 50",
    ]


def _direct_resolver_lines(text):
    """Return the ``direct-dns`` resolver, one line each, without indentation.

    The ``letsencrypt`` resolver's email (Traefik 2.11 registers a new
    ACME account at every start when it is empty) and its ``caServer``
    when that line is not commented: it is how QA asks staging.

    :param str text: the ``traefik.yml`` it goes into.
    :rtype: list
    """
    email = _letsencrypt_value(text, "email")
    ca_server = _letsencrypt_value(text, "caServer")
    lines = [f"direct-dns:  {DIRECT_MARK}", "  acme:"]
    if email:
        lines.append(f'    email: "{email}"')
    lines.append('    storage: "/etc/traefik/acme/acme.json"')
    if ca_server:
        lines.append(f'    caServer: "{ca_server}"')
    lines += [
        "    dnsChallenge:",
        "      provider: cloudflare",
        "      resolvers:",
        '        - "1.1.1.1:53"',
        '        - "8.8.8.8:53"',
    ]
    return lines


def patch_traefik_yml_direct(traefik_yml, port):
    """Return ``traefik.yml`` with the direct entrypoint and its resolver.

    Replaces what an earlier render added (``DIRECT_MARK``); a *port* of
    0 only removes it. The entrypoint goes first under ``entryPoints``,
    the resolver first under ``certificatesResolvers``, indented as the
    file indents its own.

    :param str traefik_yml: the static configuration rendered so far.
    :param int port: the host's direct port, 0 for none.
    :rtype: str
    """
    text = strip_marked_blocks(traefik_yml or "", DIRECT_MARK)
    if not (text and port):
        return text
    entry = re.search(r"^entryPoints:[ \t]*\n(?P<indent>[ \t]+)\S", text, re.MULTILINE)
    if not entry:
        return text
    indent = entry.group("indent")
    at = entry.start("indent")
    text = text[:at] + "".join(
        f"{indent}{line}\n" for line in _direct_entrypoint_lines(port)
    ) + text[at:]
    lines = _direct_resolver_lines(text)
    resolvers = re.search(
        r"^certificatesResolvers:[ \t]*\n(?P<indent>[ \t]+)\S", text, re.MULTILINE,
    )
    if resolvers:
        indent = resolvers.group("indent")
        at = resolvers.start("indent")
        return text[:at] + "".join(f"{indent}{line}\n" for line in lines) + text[at:]
    tail = "" if text.endswith("\n") else "\n"
    return (
        text + tail + f"certificatesResolvers:  {DIRECT_MARK}\n"
        + "".join(f"  {line}\n" for line in lines)
    )


def _add_proxy_environment(text, key, value):
    """Return *text* with ``key: value`` in the ``proxy`` service's environment.

    Under the service's own ``environment`` when it has one (a second key
    of that name is a compose file Docker refuses), after its ``image``
    otherwise.

    :param str text: a compose file.
    :rtype: str
    """
    service = re.search(
        r"^(?P<indent>[ \t]+)proxy:[ \t]*\n(?P<child>[ \t]+)\S", text, re.MULTILINE,
    )
    if not service:
        return text
    child = service.group("child")
    start = service.start("child")
    end = len(text)
    for line in re.finditer(r"^(?P<i>[ \t]*)\S", text[start:], re.MULTILINE):
        if len(line.group("i")) < len(child):
            end = start + line.start()
            break
    block = text[start:end]
    existing = re.search(
        rf"^{re.escape(child)}environment:[ \t]*\n", block, re.MULTILINE,
    )
    if existing:
        at = start + existing.end()
        listed = re.match(r"[ \t]*- ", block[existing.end():])
        item = f"- {key}={value}" if listed else f"{key}: {value}"
        return text[:at] + f"{child}  {item}  {DIRECT_MARK}\n" + text[at:]
    image = re.search(rf"^{re.escape(child)}image:[^\n]*\n", block, re.MULTILINE)
    at = start + (image.end() if image else 0)
    return (
        text[:at] + f"{child}environment:  {DIRECT_MARK}\n"
        + f"{child}  {key}: {value}\n" + text[at:]
    )


def patch_inverseproxy_direct(content, port, with_token):
    """Return the proxy's compose file with the direct port and the DNS token.

    The port on IPv4 only (the names have no AAAA). The token as a file
    mounted read-only and named in ``CF_DNS_API_TOKEN_FILE``: its value is
    never in this file, on a command line or in a job's log.

    :param str content: the compose file rendered so far.
    :param int port: the host's direct port, 0 for none.
    :param bool with_token: mount the DNS token.
    :rtype: str
    """
    text = strip_marked_blocks(content or "", DIRECT_MARK)
    if not text:
        return text
    if port:
        text = re.sub(
            r'^(?P<indent>[ \t]+)- "443:443"[^\n]*\n',
            lambda m: (
                f'{m.group(0)}{m.group("indent")}- "0.0.0.0:{int(port)}:{int(port)}"'
                f"  {DIRECT_MARK}\n"
            ),
            text, count=1, flags=re.MULTILINE,
        )
    if with_token:
        text = re.sub(
            r"^(?P<indent>[ \t]+)- \./traefik\.yml:/etc/traefik/traefik\.yml:ro"
            r"[^\n]*\n",
            lambda m: (
                f"{m.group(0)}{m.group('indent')}- ./secrets:/etc/traefik/secrets:ro"
                f"  {DIRECT_MARK}\n"
            ),
            text, count=1, flags=re.MULTILINE,
        )
        text = _add_proxy_environment(
            text, "CF_DNS_API_TOKEN_FILE", DIRECT_TOKEN_IN_PROXY,
        )
    return text


def publishes_direct_port(compose, port):
    """Whether *compose* publishes the direct *port* as this module adds it.

    ``patch_inverseproxy_direct`` puts the port after the compose file's
    ``- "443:443"`` line; a file that writes that line another way gets
    nothing, and the proxy would start without the port.

    :param str compose: the proxy's compose file as rendered.
    :param int port: the direct port.
    :rtype: bool
    """
    published = re.escape(f'- "0.0.0.0:{int(port)}:{int(port)}"')
    return bool(re.search(
        rf"^[ \t]+{published}[ \t]+{re.escape(DIRECT_MARK)}$",
        compose or "", re.MULTILINE,
    ))


def install_direct_yml_command(staged):
    """Return the shell that puts a staged ``direct.yml`` in place.

    Only when it changed, and atomically: copied next to the watched
    directory first, then renamed into it, because Traefik's file watcher
    reads a half-written file as it finds it.

    :param str staged: where the job uploaded the document.
    :rtype: str
    """
    quoted = shlex.quote(staged)
    return (
        "mkdir -p ~/traefik/dynamic"
        f" && if cmp -s {quoted} {DIRECT_DYNAMIC_FILE};"
        " then echo 'direct.yml unchanged';"
        f" else cp {quoted} ~/traefik/.direct.yml.new"
        f" && mv -f ~/traefik/.direct.yml.new {DIRECT_DYNAMIC_FILE}"
        " && echo 'direct.yml updated'; fi"
        f"; rc=$?; rm -f {quoted}; exit $rc"
    )


def install_dns_token_command(staged):
    """Return the shell that puts the staged DNS token where the proxy reads it.

    A host that does not need the token gets the file removed: it exists
    only while something on the host asks for a certificate by DNS-01.

    :param str staged: where the job uploaded the token (0600), or would
        have.
    :rtype: str
    """
    quoted = shlex.quote(staged)
    target = f"~/traefik/secrets/{DIRECT_TOKEN_NAME}"
    return (
        "mkdir -p ~/traefik/secrets && chmod 700 ~/traefik/secrets"
        f" && if [ -s {quoted} ]; then mv {quoted} {target}"
        f" && chmod 600 {target}; else rm -f {target}; fi"
    )


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

    def _shipped_traefik_yml(self):
        """Return the static file, with the direct entrypoint when there is a port.

        The port asked for, which Full Setup publishes. A job that leaves
        the compose file alone (the proxy-settings push) renders with
        ``direct_port_applied`` in the context: the port the running
        compose file publishes, or the entrypoint would listen on a port
        nothing publishes and the firewall does not admit.

        :rtype: str
        """
        port = (
            self.direct_port_shipped
            if self.env.context.get("direct_port_applied")
            else self.direct_port
        )
        return patch_traefik_yml_direct(super()._shipped_traefik_yml(), port)

    def _shipped_inverseproxy_yaml(self):
        """Return the proxy's compose file, with the direct port and the token.

        :rtype: str
        """
        return patch_inverseproxy_direct(
            super()._shipped_inverseproxy_yaml(),
            self.direct_port,
            self._needs_dns01() and self._has_dns01_token(),
        )

    def _asks_wildcard_by_dns(self):
        """Whether this host asks for a wildcard certificate by DNS challenge.

        Never in core. The SaaS layer answers for its catch-all host.

        :rtype: bool
        """
        self.ensure_one()
        return False

    def _needs_dns01(self):
        """Whether this host's proxy needs the DNS token.

        For the direct names' certificates, or for a wildcard.

        :rtype: bool
        """
        self.ensure_one()
        return bool(self.direct_port) or self._asks_wildcard_by_dns()

    def _has_dns01_token(self):
        """Whether the DNS token for certificates is stored.

        :rtype: bool
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        return bool(settings.acme_dns_cloudflare_token)

    @api.model
    def _direct_convergence_crons(self):
        """Return the crons that converge on a change of the direct path.

        Core: the agents' reconciliation. The SaaS layer adds its tenants'.

        :return: XML ids.
        :rtype: list
        """
        return ["incubacloud.cron_reconcile_observability"]

    @api.model
    def _direct_path_changed(self, reason):
        """Re-apply, as soon as this commits, what depends on the direct path.

        The crons run in a transaction of their own, which sees what this
        one changed; deciding here would read a state written on another
        cursor before it is visible.

        :param str reason: for the log.
        """
        _logger.info("[direct] %s: converging", reason)
        for xmlid in self._direct_convergence_crons():
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()

    def _direct_published_names(self):
        """Return the direct names this host serves, and how to check each.

        Core: the central's, on the central's host. The SaaS layer adds
        the panel's own name on the host whose address is the panel's.

        :return: ``[{"prefix", "name", "checks"}]``, checks as
            ``direct_probe.sh`` takes them.
        :rtype: list
        """
        self.ensure_one()
        settings = self.env["cloud.settings"].sudo()._get_system()
        name = (settings.metrics_central_direct_host or "").strip().lower()
        if not (name and settings.metrics_central_host_id == self):
            return []
        return [{
            "prefix": "metrics_central_direct",
            "name": name,
            "checks": ["POST:/w/api/v1/write:401", "GET:/flags:403"],
        }]

    def _direct_recently_refused(self):
        """Whether this host turned the direct path down in the last six hours.

        :rtype: bool
        """
        self.ensure_one()
        refused = self.direct_refused_at
        return bool(refused and refused > fields.Datetime.now() - _REFUSED_FOR)

    def _direct_write_offer(self):
        """Return the direct remote-write URL to offer this host's agents, or ''.

        Offered while the central's direct name works, this host's own
        address is applied on the central's host, the host is not marked
        "Use the public path" and it did not refuse the path in the last
        six hours. The install still tries it from the host first.

        :rtype: str
        """
        self.ensure_one()
        if self.direct_path_opt_out or self._direct_recently_refused():
            return ""
        settings = self.env["cloud.settings"].sudo()._get_system()
        url = settings._direct_central_write_url()
        central = settings.metrics_central_host_id
        if not url or not central._direct_admits(self.sudo().ip_address):
            return ""
        return url

    def _metrics_remote_write_url(self):
        """Return the URL this host's agents should be installed with.

        The direct one on offer, else the remote-write URL in Settings:
        the public path is the base and the way back.

        :rtype: str
        """
        self.ensure_one()
        settings = self.env["cloud.settings"].sudo()._get_system()
        public = (settings.metrics_remote_write_url or "").strip()
        return self._direct_write_offer() or public

    def _direct_reach_probes(self, write_url):
        """Return what the host tries before taking the direct path.

        Core: the write URL answers 401 without a credential (vmauth). The
        SaaS layer adds the panel's direct name, which the panels on this
        host would call.

        :param str write_url: the direct URL on offer.
        :return: ``[{"url", "method", "status", "prefix"}]``.
        :rtype: list
        """
        self.ensure_one()
        return [{
            "url": write_url, "method": "POST", "status": 401,
            "prefix": "metrics_central_direct",
        }]

    def _direct_allowlist_pending(self):
        """Whether the list in force on this host lags the one it should have.

        :rtype: bool
        """
        self.ensure_one()
        if not self.direct_port_shipped:
            return False
        shipped = (self.direct_allowlist_shipped or "").split()
        return shipped != self._direct_peers_entries()

    def _direct_untried_probes(self):
        """Return the direct checks on offer that this host's last install never tried.

        Only after an install whose checks all answered: one that failed
        waits out its six hours instead.

        :return: URLs.
        :rtype: list
        """
        self.ensure_one()
        offer = self._direct_write_offer()
        if not (offer and self.direct_reach_ok):
            return []
        tried = set((self.direct_reach_urls or "").split())
        return [
            probe["url"] for probe in self._direct_reach_probes(offer)
            if probe["url"] not in tried
        ]

    def _direct_silent(self, now, others):
        """Whether this host, installed on the direct path, has gone quiet.

        Quiet for 20 minutes, its agents not applied in the last hour, and,
        when *others* write directly too, at least one of them up to date:
        all of them quiet at once is the central, not the hosts.

        :param datetime now: the reconciliation's time.
        :param others: the other hosts installed on the direct path.
        :rtype: bool
        """
        self.ensure_one()
        seen = self.metrics_last_seen
        quiet = not seen or seen < now - _SILENT_AFTER
        since = self.metrics_agents_since
        recent = since and since > now - _REAPPLY_EVERY
        if not quiet or recent:
            return False
        if not others:
            return True
        return any(
            other.metrics_last_seen
            and other.metrics_last_seen >= now - _SILENT_AFTER
            for other in others
        )

    @api.model
    def _reconcile_direct_agents(self):
        """Re-apply the agents whose URL no longer matches the direct path.

        Three cases: a host installed with another URL than the one it is
        offered now (the path came, went, or the host turned it down six
        hours ago); a host with a direct check it never tried (the panel's
        name appeared after its install); a host on the direct path gone
        quiet while the central is healthy (``_direct_silent``), which the
        install tries again and drops to the public path if it must. An
        empty recorded URL is the remote-write URL of before this version.

        :return: how many installs were queued.
        :rtype: int
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return 0
        public = (settings.metrics_remote_write_url or "").strip()
        now = fields.Datetime.now()
        hosts = self.sudo().search([
            ("metrics_agents_state", "=", "installed"), ("active", "=", True),
        ])
        direct = hosts.filtered(
            lambda h: h.metrics_agents_write_url
            and h.metrics_agents_write_url != public,
        )
        healthy = settings._direct_central_healthy()
        queued = 0
        for host in hosts:
            if not host._observability_target():
                continue
            installed = host.metrics_agents_write_url or public
            if installed != host._metrics_remote_write_url():
                reason = "direct path changed"
            elif host._direct_untried_probes():
                reason = "direct path to try"
            elif (
                host in direct and healthy
                and host._direct_silent(now, direct - host)
            ):
                reason = "quiet on the direct path"
            else:
                continue
            if host._enqueue_observability(reason):
                queued += 1
        if queued:
            _logger.info("[direct] re-applied the agents of %s host(s)", queued)
        return queued

    def _record_direct_reach(self, report):
        """Keep what this host's own try of the direct path said.

        A good try records the URLs that answered. A failed one, while
        every name it tried is in force (the settings'
        ``_direct_name_in_force``), is the host's: the direct path is not
        offered again for six hours, and a second one in a row raises
        ``direct_path_unreachable_from_host``. A failure while a name is
        not in force is the central's or the panel's, not the host's: it
        is noted as inconclusive, with no wait and no alert.

        :param list report: as ``_direct_probe.reach_report`` returns it.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        settings = self.env["cloud.settings"].sudo()._get_system()
        Alert = self.env["cloud.alert"].sudo()
        passed = [a["url"] for a in report if a["status"] == a["expected"]]
        failed = [a for a in report if a["status"] != a["expected"]]
        vals = {"direct_reach_urls": "\n".join(passed), "direct_reach_at": now}
        if not failed:
            vals.update(
                direct_reach_ok=True, direct_reach_failures=0,
                direct_reach_reason=False,
            )
            self.sudo().write(vals)
            Alert.resolve_alert("direct_path_unreachable_from_host", host=self)
            return
        reason = _direct_probe.reach_reason(failed[0])
        vals["direct_reach_ok"] = False
        if not all(settings._direct_name_in_force(a["prefix"]) for a in failed):
            vals["direct_reach_reason"] = f"inconclusive: {reason}"
            self.sudo().write(vals)
            return
        failures = (self.direct_reach_failures or 0) + 1
        vals.update(
            direct_reach_failures=failures, direct_refused_at=now,
            direct_reach_reason=reason,
        )
        self.sudo().write(vals)
        if failures >= 2:
            Alert.raise_alert(
                "direct_path_unreachable_from_host",
                _(
                    "%(host)s does not reach the direct path (%(reason)s); its "
                    "agents use the public path.",
                    host=self.name, reason=reason,
                ),
                host=self,
            )

    def _enqueue_direct_sync(self, reason, chained=False):
        """Queue ``sync_direct_path`` on this host, swallowing failures.

        :param str reason: for the log.
        :param bool chained: called from a job of this host that is still
            running: the sync belongs to it and goes to the head of the
            queue.
        :return: the job id, or False.
        """
        self.ensure_one()
        try:
            return self.env["cloud.job"].sudo().enqueue(
                self.id, False, "sync_direct_path", bypass_running_check=chained,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the caller
            _logger.warning(
                "[direct] could not queue the direct list on %s (%s): %s",
                self.name, reason, exc,
            )
            return False

    @api.model
    def _direct_sync_targets(self):
        """Return the hosts whose proxy publishes a direct port.

        :rtype: cloud.host
        """
        return self.sudo().search([
            ("active", "=", True), ("direct_port_shipped", ">", 0),
        ])

    @api.model
    def _direct_sync_everywhere(self, reason):
        """Queue the direct list on every host that publishes a direct port.

        :param str reason: for the log.
        """
        for host in self._direct_sync_targets():
            host._enqueue_direct_sync(reason)

    @api.model_create_multi
    def create(self, vals_list):
        """A host born with a global address joins the direct list at once."""
        hosts = super().create(vals_list)
        if any(global_ipv4(host.sudo().ip_address) for host in hosts):
            self._direct_sync_everywhere("host created")
        return hosts

    def write(self, vals):
        """A new address or an archived host moves the direct list.

        And the public-path switch re-applies what runs on the host.
        """
        result = super().write(vals)
        if {"ip_address", "active"} & set(vals):
            self.env["cloud.host"]._direct_sync_everywhere(
                "host address or state changed",
            )
        if "direct_path_opt_out" in vals:
            self.env["cloud.host"]._direct_path_changed(
                "public path switched on a host",
            )
        return result

    def unlink(self):
        """A deleted host leaves the direct list at once."""
        result = super().unlink()
        self.env["cloud.host"]._direct_sync_everywhere("host deleted")
        return result

    @api.model
    def _cron_sync_direct_path(self):
        """Keep every direct port's list applied, and its names probed.

        Queues the job on a host whose list in force differs from the one
        it should have, or whose last good run is over an hour old (about
        24 runs a day per publishing host, like ``host_probe``). A list
        behind for over 30 minutes raises ``direct_allowlist_stale``; in
        step again, it is closed.
        """
        now = fields.Datetime.now()
        desired = self._direct_peers_entries()
        Alert = self.env["cloud.alert"].sudo()
        for host in self._direct_sync_targets():
            shipped = (host.direct_allowlist_shipped or "").split()
            behind = shipped != desired
            if not behind:
                if host.direct_allowlist_stale_since:
                    host.write({"direct_allowlist_stale_since": False})
                Alert.resolve_alert("direct_allowlist_stale", host=host)
            elif not host.direct_allowlist_stale_since:
                host.write({"direct_allowlist_stale_since": now})
            elif now - host.direct_allowlist_stale_since > _STALE_AFTER:
                Alert.raise_alert(
                    "direct_allowlist_stale",
                    _(
                        "The direct path's list on %(host)s is behind: "
                        "%(n)s address(es) not admitted yet use the public "
                        "path.",
                        host=host.name, n=len(set(desired) - set(shipped)),
                    ),
                    host=host,
                )
            synced = host.direct_allowlist_synced_at
            if behind or not synced or now - synced >= _RESYNC_EVERY:
                host._enqueue_direct_sync("direct list cron")
