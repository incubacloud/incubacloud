import asyncio
import base64
import hashlib
import json
import logging
from contextlib import asynccontextmanager

import asyncssh

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import file_open

from .cloud_host_whitelist import DEFAULT_WHITELIST
from .encrypted_char import EncryptedChar
from .password_utils import generate_password
from .transport import SSHTransport

_logger = logging.getLogger(__name__)


def parse_memory_to_gb(mem_str):
    """Convert Docker memory strings like '2g', '512m', '256m' to float GB."""
    if not mem_str:
        return 0.0
    s = str(mem_str).strip().lower()
    if not s:
        return 0.0
    suffix = s[-1]
    try:
        if suffix == "g":
            return float(s[:-1])
        if suffix == "m":
            return float(s[:-1]) / 1024.0
        if suffix == "k":
            return float(s[:-1]) / (1024.0**2)
        return float(s) / (1024.0**3)
    except ValueError:
        return 0.0


def _read_traefik_template(filename):
    """Read a Traefik template file bundled with the module."""
    try:
        with file_open(f"incubacloud/data/traefik/{filename}") as f:
            return f.read()
    except Exception as e:
        _logger.warning("Could not read Traefik template %s: %s", filename, e)
        return ""


class CloudHost(models.Model):
    _name = "cloud.host"
    _inherit = ["cloud.security.mixin", "cloud.audit.tracked.mixin"]
    _description = "Cloud Host"

    name = fields.Char(
        required=True,
        translate=True,
    )
    type = fields.Selection(
        selection=[
            ("self_hosted", "Self-hosted"),
        ],
        string="Host Type",
        default="self_hosted",
        help="Type of the cloud host. Other modules may extend this selection.",
    )
    ip_address = fields.Char(
        string="IP Address",
        required=True,
        help="The IP address of the cloud host.",
    )
    port = fields.Integer(
        string="Port",
        required=True,
        default=22,
        help="The port to connect to the cloud host.",
    )
    password = EncryptedChar(
        string="Password",
        required=True,
        groups="incubacloud.group_cloud_developer",
        help="Password for accessing the cloud host.",
    )
    key_file = EncryptedChar(
        string="SSH Key",
        groups="incubacloud.group_cloud_developer",
        help="SSH private key for accessing the cloud host.",
    )
    known_hosts_key = fields.Text(
        string="Known Hosts Key",
        help=(
            "Server host public key in known_hosts format, captured via "
            '"Trust SSH Key". All SSH connections verify against this key.'
        ),
    )
    user = fields.Char(
        string="User",
        required=True,
        help="Username for accessing the cloud host.",
    )
    login_type = fields.Selection(
        selection=[
            ("password", "Password"),
            ("ssh_key", "SSH Key"),
        ],
        string="Login Type",
        required=True,
        default="ssh_key",
        help="Method used to authenticate to the cloud host.",
    )
    description = fields.Text(
        string="Description",
        translate=True,
    )
    tag_ids = fields.Many2many(
        comodel_name="cloud.host.tag",
        relation="cloud_host_tag_rel",
        column1="host_id",
        column2="tag_id",
        string="Tags",
    )
    instance_ids = fields.One2many(
        comodel_name="cloud.instance",
        inverse_name="host_id",
        string="Instances",
    )
    whitelist_ids = fields.One2many(
        comodel_name="cloud.host.whitelist",
        inverse_name="host_id",
        string="Whitelist",
    )
    status = fields.Selection(
        selection=[
            ("compatible", "Compatible"),
            ("degraded", "Degraded"),
            ("unsupported", "Unsupported"),
            ("checking", "Checking"),
            ("unknown", "Unknown"),
        ],
        string="Status",
        default="unknown",
        help="Compatibility status of the cloud host.",
    )
    active = fields.Boolean(default=True)
    traefik_deployed = fields.Boolean(
        string="Traefik Deployed",
        default=False,
        help="Whether the Traefik reverse proxy has been deployed on this host.",
    )

    alert_ids = fields.One2many("cloud.alert", "host_id", string="Alerts")

    # ── Hardening (layer 1: in-host SSH/firewall recipe) ──────────────────
    # Applied by ``host_hardening_executor`` via the Ansible playbook
    # ``ansible/playbooks/host_hardening.yml`` (sshd drop-in, nftables,
    # fail2ban, sysctl, unattended-upgrades, docker daemon, journald,
    # auditd). When ``hardened`` is True the host is reached as the
    # non-root operator user on the randomized SSH port stored in ``port``.
    # The network-edge firewall (layer 2, e.g. Hetzner Cloud Firewall) is
    # NOT modelled here — it follows the provider and lives in the SaaS
    # layer (``incubacloud_saas_manager``).
    hardened = fields.Boolean(
        string="Hardened",
        default=False,
        readonly=True,
        copy=False,
        help="Set by host_hardening_executor on success. When True the host "
             "is reached as the non-root operator user on the randomized SSH "
             "port stored in ``port``.",
    )
    hardened_at = fields.Datetime(
        string="Hardened at",
        readonly=True,
        copy=False,
    )
    auto_security_updates = fields.Boolean(
        string="Auto Security Updates",
        default=True,
        help="Enable unattended-upgrades for OS security patches with an "
             "automatic reboot at 04:00 when a kernel update lands. Disable "
             "for hosts whose downtime windows must be controlled manually.",
    )
    allowed_ssh_ips = fields.Char(
        string="Allowed SSH IPs",
        help="Comma-separated IPs/CIDRs allowed to SSH in addition to the "
             "operator IP (prefilled from the request that triggers "
             "hardening). Edit and re-run hardening to push the change to "
             "the host (idempotent — updates the in-host nftables allowlist).",
    )

    # ── Server specs + disk health (updated by cron every 5 min) ──────────
    cpu_cores = fields.Integer(string="CPU Cores", default=0)
    ram_total_gb = fields.Float(string="RAM Total (GB)", default=0, digits=(6, 1))
    disk_usage = fields.Float(string="Disk Usage %", default=0)
    disk_free_gb = fields.Float(string="Free Disk (GB)", default=0)
    last_probed = fields.Datetime(string="Last Specs Probe")

    # ── Auto-assign & resource tracking ─────────────────────────────────────
    exclude_from_autoassign = fields.Boolean(
        string="Exclude from auto-assign",
        default=False,
        help="If checked, this host will not be considered for automatic "
        "instance placement.",
    )
    allocated_cpus = fields.Float(
        string="Allocated CPUs",
        compute="_compute_resource_availability",
    )
    allocated_ram_gb = fields.Float(
        string="Allocated RAM (GB)",
        compute="_compute_resource_availability",
    )
    available_cpus = fields.Float(
        string="Available CPUs",
        compute="_compute_resource_availability",
    )
    available_ram_gb = fields.Float(
        string="Available RAM (GB)",
        compute="_compute_resource_availability",
    )

    usage_ratio = fields.Float(
        string="Usage Ratio",
        compute="_compute_usage_ratio",
        help="Fraction in [0, 1] of allocated/total on the most "
        "constrained dimension (CPU or RAM). Inheriting modules "
        "can read this metric to drive auto-provisioning decisions.",
    )

    @api.depends(
        "cpu_cores",
        "ram_total_gb",
        "instance_ids.odoo_cpus",
        "instance_ids.db_cpus",
        "instance_ids.backup_cpus",
        "instance_ids.smtp_cpus",
        "instance_ids.odoo_memory_limit",
        "instance_ids.db_memory_limit",
        "instance_ids.backup_memory_limit",
        "instance_ids.smtp_memory_limit",
    )
    def _compute_resource_availability(self):
        for host in self:
            total_cpus = 0.0
            total_ram = 0.0
            for inst in host.instance_ids:
                if not host._consumes_capacity(inst):
                    continue
                total_cpus += (
                    inst.odoo_cpus + inst.db_cpus + inst.backup_cpus + inst.smtp_cpus
                )
                total_ram += sum(
                    parse_memory_to_gb(m)
                    for m in (
                        inst.odoo_memory_limit,
                        inst.db_memory_limit,
                        inst.backup_memory_limit,
                        inst.smtp_memory_limit,
                    )
                )
            host.allocated_cpus = total_cpus
            host.allocated_ram_gb = total_ram
            host.available_cpus = host.cpu_cores - total_cpus
            host.available_ram_gb = host.ram_total_gb - total_ram

    def _consumes_capacity(self, inst):
        """Whether ``inst`` counts against this host's CPU/RAM.

        Hook for inheriting modules. The base implementation counts
        every instance attached to the host; subclasses can override
        to exclude instances whose containers are intentionally
        stopped and therefore do not reserve live capacity.
        """
        self.ensure_one()
        return True

    @api.depends("allocated_cpus", "cpu_cores", "allocated_ram_gb", "ram_total_gb")
    def _compute_usage_ratio(self):
        for host in self:
            cpu = host.allocated_cpus / host.cpu_cores if host.cpu_cores else 0
            ram = host.allocated_ram_gb / host.ram_total_gb if host.ram_total_gb else 0
            host.usage_ratio = max(cpu, ram)

    _MIN_DISK_FREE_GB = 10

    #: Active alert codes that mark a host as measurably unhealthy for
    #: placement. All are produced by the metrics pipeline from real
    #: readings; ``metrics_host_absent`` is included because a host that
    #: stopped reporting is an unknown, and silence is not health.
    _PLACEMENT_VETO_ALERT_CODES = (
        "metrics_disk_critical",
        "metrics_memory_high",
        "metrics_cpu_saturated",
        "metrics_host_absent",
    )

    def _placement_vetoed_hosts(self):
        """Return the subset of ``self`` with an active overload alert.

        Used by :meth:`select_best_host` to keep placement away from
        hosts the metrics pipeline has *measured* as overloaded or
        silent, complementing the declared-allocation scoring.
        """
        if not self:
            return self.browse()
        alerts = self.env["cloud.alert"].sudo().search(
            [
                ("code", "in", list(self._PLACEMENT_VETO_ALERT_CODES)),
                ("state", "=", "active"),
                ("host_id", "in", self.ids),
            ]
        )
        return alerts.mapped("host_id") & self

    @api.model
    def select_best_host(
        self, required_cpus=3.75, required_ram_gb=3.75, additional_domain=None
    ):
        """Return the best eligible host for a new instance.

        Prefers hosts that fit the requested ``required_cpus`` and
        ``required_ram_gb`` (scored by remaining balanced headroom).
        When no eligible host fits, falls back to the one with the
        most headroom in the most constrained dimension — callers can
        place instances while any inheriting auto-provisioning layer
        reacts to the saturation signal independently.
        Only returns empty when there is no compatible host at all.

        ``additional_domain`` is an optional extra search domain ANDed
        into the eligibility filter (and therefore the fallback too), so
        inheriting layers can constrain placement without forking this
        method — e.g. the SaaS layer passes ``[('pool_id', '=', ...)]``
        to keep free and paid tenants on separate hardware.
        """
        domain = [
            ("status", "=", "compatible"),
            ("traefik_deployed", "=", True),
            ("exclude_from_autoassign", "=", False),
            ("disk_free_gb", ">=", self._MIN_DISK_FREE_GB),
        ]
        if additional_domain:
            domain += additional_domain
        eligible = self.search(domain)
        if not eligible:
            return self.browse()

        # Real-signal veto: the scoring below works on *declared*
        # allocation (Docker limits), which says nothing about actual
        # load. When the metrics pipeline has raised a measured-overload
        # alert on a host, prefer the others; if every candidate is
        # vetoed, fall through with all of them so placement still works
        # and the saturation alerts remain the operator's signal. With
        # metrics disabled there are no such alerts and behavior is
        # unchanged (fail-safe).
        vetoed = eligible._placement_vetoed_hosts()
        if vetoed and eligible - vetoed:
            eligible -= vetoed

        best_fit, best_fit_score = None, -1.0
        best_fallback, best_fallback_headroom = None, float("-inf")
        for host in eligible:
            cpu_free = host.available_cpus
            ram_free = host.available_ram_gb
            cpu_r = cpu_free / host.cpu_cores if host.cpu_cores else 0
            ram_r = ram_free / host.ram_total_gb if host.ram_total_gb else 0
            headroom = min(cpu_r, ram_r)

            if cpu_free - required_cpus >= 0 and ram_free - required_ram_gb >= 0:
                avail_cpu = cpu_free - required_cpus
                avail_ram = ram_free - required_ram_gb
                acr = avail_cpu / host.cpu_cores if host.cpu_cores else 0
                arr = avail_ram / host.ram_total_gb if host.ram_total_gb else 0
                score = 2 * min(acr, arr) + acr + arr
                if score > best_fit_score:
                    best_fit, best_fit_score = host, score

            if headroom > best_fallback_headroom:
                best_fallback, best_fallback_headroom = host, headroom

        return best_fit or best_fallback or self.browse()

    # ── Traefik configuration ───────────────────────────────────────────────
    wildcard_domain = fields.Char(
        string="Wildcard Domain",
        required=True,
        help="Base domain for wildcard certificates (e.g. *.example.com).",
    )
    traefik_panel_password = EncryptedChar(
        string="Traefik Panel Password",
        required=True,
        groups="incubacloud.group_cloud_developer",
        help="Password for the Traefik dashboard.",
    )
    traefik_config_yml = fields.Text(
        string="config.yml",
        required=True,
        default=lambda self: _read_traefik_template("config.yml"),
        help="Content of the Traefik config.yml file.",
    )
    traefik_inverseproxy_yaml = fields.Text(
        string="inverseproxy.yaml",
        required=True,
        default=lambda self: _read_traefik_template("inverseproxy.yaml"),
        help="Content of the inverseproxy.yaml file.",
    )
    traefik_yml = fields.Text(
        string="traefik.yml",
        required=True,
        default=lambda self: _read_traefik_template("traefik.yml"),
        help="Content of the traefik.yml file.",
    )

    # ── Config drift: saved host config vs what full_setup shipped ────────
    # Same mechanism as on cloud.instance: full_setup records the hash of
    # what it actually uploaded; the compute compares it against the
    # current snapshot so an edited traefik.yml (or wildcard, panel
    # password, whitelist) stops reading as "applied" when it is not.

    applied_config_hash = fields.Char(
        copy=False,
        readonly=True,
        help="Hash of the host config snapshot the last successful "
             "full setup shipped. Compared against the current snapshot "
             "to surface unapplied changes.",
    )
    config_dirty = fields.Boolean(
        compute="_compute_config_dirty",
        help="True when the saved host configuration differs from what "
             "the last full setup shipped; re-running full setup "
             "applies it.",
    )

    def _config_snapshot_fields(self):
        """Host fields full_setup renders and uploads.

        Override in modules whose full_setup variant renders more state
        (e.g. the SaaS catch-all flag changes the Traefik config) and
        extend the list via ``super()``.
        """
        return [
            "wildcard_domain",
            "traefik_yml",
            "traefik_inverseproxy_yaml",
            "traefik_panel_password",
        ]

    def _render_config_snapshot(self):
        """Deterministic dict of everything full_setup ships for this
        host: the declared fields plus the whitelist hostname set."""
        self.ensure_one()
        host = self.sudo()
        snap = {f: host[f] or "" for f in self._config_snapshot_fields()}
        snap["whitelist"] = sorted(host.whitelist_ids.mapped("hostname"))
        return snap

    def _config_snapshot_hash(self):
        """SHA-256 hex digest of the canonical-JSON host snapshot."""
        self.ensure_one()
        raw = json.dumps(
            self._render_config_snapshot(), sort_keys=True, default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    # Same dependency stance as cloud.instance: anchor-only on purpose;
    # HTTP requests read through a fresh cache, tests invalidate.
    @api.depends("applied_config_hash")
    def _compute_config_dirty(self):
        """Dirty only when a full_setup recorded a hash and it no longer
        matches; hosts that never completed a setup are not flagged."""
        for host in self:
            applied = host.applied_config_hash
            if not applied:
                host.config_dirty = False
                continue
            try:
                host.config_dirty = applied != host._config_snapshot_hash()
            except Exception:
                # Same stance as the instance compute: a snapshot that
                # cannot render must not break the detail page.
                _logger.warning(
                    "config_dirty: snapshot render failed for host %s",
                    host.id, exc_info=True,
                )
                host.config_dirty = False

    def _subdomain_suffix(self):
        """Return the wildcard domain ready to suffix an instance subdomain.

        ``wildcard_domain`` is stored in wildcard form (e.g.
        ``*.example.com``); a concrete hostname is built as
        ``f"{subdomain}.{host._subdomain_suffix()}"``. Stripping the leading
        ``*.`` turns ``*.example.com`` into ``example.com`` so the result is a
        valid hostname (``sub.example.com``) instead of ``sub.*.example.com``.
        A plain domain without the wildcard label is returned unchanged.
        """
        self.ensure_one()
        return (self.wildcard_domain or "").removeprefix("*.")

    # ── SSH connection helpers ──────────────────────────────────────────────

    def _get_ssh_private_key_bytes(self):
        """Return ``key_file`` as raw PEM/OpenSSH bytes for asyncssh.

        ``key_file`` is stored base64-encoded — the SPA uploads it via
        ``btoa()`` ([host_detail.js]) and the auto-provisioner stamps
        new VPS keys the same way. ``asyncssh.connect``'s ``client_keys``
        accepts bytes containing raw key data, but a *string* gets
        treated as a filename and triggers ``open_file()``. So we always
        return bytes here and centralise the decode.
        """
        self.ensure_one()
        if not self.key_file:
            return b""
        return base64.b64decode(self.key_file)

    def _capture_known_host_key(self):
        """Connect once bypassing host-key verification (TOFU) and store
        the server public key in ``known_hosts_key``.

        Used both by the manual 'Trust SSH Key' UI action and by autoprov
        right after a freshly contracted VPS boots — the latter has no
        human in the loop, so the capture has to happen automatically.

        Raises ``asyncssh.Error`` / ``OSError`` on connection failure.
        """
        self.ensure_one()
        connect_kw = dict(
            host=self.ip_address,
            port=self.port,
            username=self.user,
            known_hosts=None,
            agent_path=None,
        )
        if self.login_type == "ssh_key" and self.key_file:
            connect_kw["client_keys"] = [self._get_ssh_private_key_bytes()]
        else:
            connect_kw["password"] = self.password
            connect_kw["client_keys"] = None

        async def _capture():
            async with asyncssh.connect(**connect_kw) as conn:
                server_key = conn.get_server_host_key()
                key_data = (
                    server_key.export_public_key(
                        "openssh",
                    )
                    .decode()
                    .strip()
                )
                prefix = (
                    f"[{self.ip_address}]:{self.port}"
                    if self.port != 22
                    else self.ip_address
                )
                return f"{prefix} {key_data}"

        loop = asyncio.new_event_loop()
        try:
            entry = loop.run_until_complete(_capture())
        finally:
            loop.close()
        # Log a SHA256 fingerprint of the captured host key so an
        # operator can verify it post-hoc against the provider's
        # console / out-of-band channel. Pure TOFU otherwise blindly
        # trusts whatever responds on first connect.
        try:
            key_b64 = entry.split(" ", 2)[-2]
            digest = hashlib.sha256(base64.b64decode(key_b64)).digest()
            fp = base64.b64encode(digest).rstrip(b"=").decode("ascii")
            _logger.info(
                "[host %s] captured SSH host key SHA256:%s "
                "(verify out-of-band against provider console)",
                self.name,
                fp,
            )
        except Exception:
            _logger.info("[host %s] captured SSH host key", self.name)
        self.sudo().write({"known_hosts_key": entry})
        return entry

    def ssh_connect_kwargs(self):
        """Return kwargs dict for ``asyncssh.connect()``.

        Centralises auth logic so every caller (executors, controllers,
        terminal sessions) shares the same behaviour.  When *password*
        auth is selected, ``client_keys`` is set to an empty list to
        prevent asyncssh from probing ``~/.ssh`` for default keys.
        """
        self.ensure_one()
        if not self.known_hosts_key:
            raise UserError(
                _(
                    "Host '%(name)s' has no trusted SSH key. "
                    "Open the host page and click 'Trust SSH Key' first.",
                    name=self.name,
                )
            )
        kwargs = dict(
            host=self.ip_address,
            port=self.port,
            username=self.user,
            known_hosts=asyncssh.import_known_hosts(self.known_hosts_key),
        )
        if self.login_type == "ssh_key" and self.key_file:
            kwargs["client_keys"] = [self._get_ssh_private_key_bytes()]
        else:
            kwargs["password"] = self.password
            # None (not []) tells asyncssh to skip default key loading.
            # With [], asyncssh falls through to load_default_keypairs()
            # which reads ~/.ssh/id_* — breaks when those files are
            # empty/invalid inside the container.
            kwargs["client_keys"] = None
        # Disable SSH agent socket probing inside the container.
        kwargs["agent_path"] = None
        # Bound both phases so a dead/unreachable host fails fast instead
        # of hanging the job indefinitely.
        kwargs["connect_timeout"] = 30
        kwargs["login_timeout"] = 30
        return kwargs

    def build_port_forward_cmd(
        self, local_port, remote_host="127.0.0.1", remote_port=None,
    ):
        """Return an ``ssh -N -L`` local port-forward command for this host.

        Builds the string an operator runs on their workstation to tunnel
        ``local_port`` to ``remote_host:remote_port`` on this host — used
        for panels pinned to loopback (e.g. the Traefik dashboard). Odoo
        never runs it; it is a UX convenience, so this only assembles the
        string from the host's connection fields.

        :param int local_port: local port to bind on the operator's machine
        :param str remote_host: address to reach *on the host* (default
            loopback)
        :param remote_port: remote port; defaults to *local_port*
        :return: the ``ssh`` command string
        :raises UserError: if the host has no ip_address / SSH user
        """
        self.ensure_one()
        if not (self.ip_address and self.user):
            raise UserError(
                _("Host '%(name)s' is missing its IP address or SSH user.",
                  name=self.name)
            )
        remote_port = remote_port or local_port
        # -N keeps the tunnel open without running a remote command.
        parts = [
            "ssh", "-N",
            "-L", f"{local_port}:{remote_host}:{remote_port}",
        ]
        if self.port and self.port != 22:
            parts += ["-p", str(self.port)]
        parts.append(f"{self.user}@{self.ip_address}")
        return " ".join(parts)

    @asynccontextmanager
    async def get_transport(self):
        """Open a connection and yield a ``BaseTransport`` for this host.

        Usage::

            async with host.get_transport() as transport:
                result = await transport.run("uname -s")

        To support a new backend, add a ``backend_type`` field and branch
        here to return the appropriate transport class.
        """
        self.ensure_one()
        connect_kw = self.ssh_connect_kwargs()
        connect_kw.update(keepalive_interval=30, keepalive_count_max=10)
        async with asyncssh.connect(**connect_kw) as conn:
            yield SSHTransport(conn)

    # ── Creation: seed default whitelist ───────────────────────────────────

    # Fields tracked in the audit log when changed (never passwords)
    _AUDIT_TRACKED_FIELDS = frozenset(
        {
            "name",
            "ip_address",
            "port",
            "user",
            "wildcard_domain",
            "active",
            "login_type",
            "traefik_deployed",
        }
    )

    # Password fields that are auto-generated when left empty
    _PASSWORD_FIELDS = ("password", "traefik_panel_password")
    # Traefik fields that should fall back to template defaults when empty
    _TRAEFIK_TEMPLATE_FIELDS = {
        "traefik_config_yml": "config.yml",
        "traefik_inverseproxy_yaml": "inverseproxy.yaml",
        "traefik_yml": "traefik.yml",
    }

    @api.model_create_multi
    def create(self, vals_list):
        self._check_can_manage_hosts()
        for vals in vals_list:
            for field in self._PASSWORD_FIELDS:
                if not vals.get(field):
                    vals[field] = generate_password()
            # Replace empty traefik fields with template defaults
            for field, filename in self._TRAEFIK_TEMPLATE_FIELDS.items():
                if field in vals and not vals[field]:
                    vals[field] = _read_traefik_template(filename)
        hosts = super().create(vals_list)
        for host in hosts:
            if not host.whitelist_ids:
                self.env["cloud.host.whitelist"].create(
                    [
                        {"host_id": host.id, "hostname": h, "sequence": i * 10}
                        for i, h in enumerate(DEFAULT_WHITELIST, 1)
                    ]
                )
            host._after_provision()
            self.env["cloud.audit.log"].sudo().create(
                {
                    "action": "Host created",
                    "host_id": host.id,
                }
            )
        return hosts

    def _after_provision(self):
        """Hook called after a host record is created.

        Override in dependent modules to run post-creation logic."""

    # ── Deletion guard ─────────────────────────────────────────────────────

    def _check_no_instances(self):
        for host in self:
            if host.instance_ids:
                raise UserError(
                    _(
                        "Cannot delete host '%s': it still has %d instance(s). "
                        "Delete all instances first."
                    )
                    % (host.name, len(host.instance_ids))
                )

    def _release_external_resources(self):
        """Lifecycle hook fired before a host is archived or unlinked.

        Override in derived modules to release any external resources
        attached to the host (cloud VMs, DNS records, remote API
        registrations, etc.). No-op by default."""

    def unlink(self):
        self._check_can_manage_hosts()
        self._check_no_instances()
        self._release_external_resources()
        for host in self:
            self.env["cloud.audit.log"].sudo().create(
                {
                    "action": "Host deleted",
                    "details": host.name,
                }
            )
        return super().unlink()

    def write(self, vals):
        self._check_can_manage_hosts()
        if vals.get("active") is False:
            self._check_no_instances()
            self.filtered("active")._release_external_resources()
        # Drop empty password values so existing stored passwords are preserved
        for field in self._PASSWORD_FIELDS:
            if field in vals and not vals[field]:
                del vals[field]
        # Endpoint change → forbid while jobs are running, then drop
        # the captured SSH host key so the next connection forces a
        # fresh TOFU. The previously captured key was bound to the
        # old (ip_address, port) pair and no longer corresponds to
        # the new endpoint.
        endpoint_changing = "ip_address" in vals or "port" in vals
        endpoint_invalidated = []
        # Executors that legitimately rotate the endpoint from inside
        # their own running job (e.g. host hardening rotates the SSH
        # port as part of its workflow) opt out of the guard with this
        # context flag. Without it the executor would self-block: its
        # own job is in ``started`` state, plus any chained job sits in
        # ``wait_dependencies``, so the active-jobs count is always ≥1.
        skip_guard = self.env.context.get("skip_endpoint_change_guard")
        if endpoint_changing and not skip_guard:
            for rec in self:
                new_ip = vals.get("ip_address", rec.ip_address)
                new_port = vals.get("port", rec.port)
                if new_ip == rec.ip_address and new_port == rec.port:
                    continue
                # Initial endpoint assignment (host without a real
                # prior IP) is not a security-relevant "change": no SSH
                # host key was captured yet, no running job targets a
                # previous endpoint, nothing to invalidate. Skip the
                # guard so provisioning executors (provision_vps,
                # manual setup) can persist the assigned IP from inside
                # their own running job without self-blocking.
                #
                # ``0.0.0.0`` is the placeholder used by on-demand host
                # creation (cloud.host.ip_address is required=True, so
                # _create_on_demand stores a sentinel until the
                # provisioning job writes the real IP).
                if not rec.ip_address or rec.ip_address == "0.0.0.0":
                    continue
                active = self.env["cloud.job"].search_count(
                    [
                        ("host_id", "=", rec.id),
                        ("state", "in", self.env["cloud.job"]._active_states),
                    ]
                )
                if active:
                    raise UserError(
                        _(
                            "Cannot change endpoint of host '%(name)s' while "
                            "%(count)d job(s) are running or pending against it. "
                            "Wait for them to finish (or cancel them) and try again.",
                        )
                        % {"name": rec.name, "count": active}
                    )
                endpoint_invalidated.append(rec.id)
            if endpoint_invalidated:
                # Don't override an explicit caller-supplied key (manager
                # may be importing a fresh known-good key on purpose).
                vals.setdefault("known_hosts_key", False)
        changed, old_snap = self._audit_snapshot(vals)
        result = super().write(vals)
        for host_id in endpoint_invalidated:
            self.env["cloud.audit.log"].sudo().create(
                {
                    "action": "SSH host key invalidated due to endpoint change",
                    "host_id": host_id,
                }
            )
        self._audit_log_changes(changed, old_snap)
        return result

    def _audit_target_vals(self):
        """Point 'Config changed' audit rows at this host."""
        self.ensure_one()
        return {"host_id": self.id}

    # ── Metrics collection (cron) ───────────────────────────────────────────

    def _ssh_ready_domain(self):
        """Domain matching hosts whose SSH layer can actually connect.

        Filters out hosts pending trust (no captured server key) or
        missing credentials for their declared ``login_type``. Skipping
        them at the cron level avoids spawning jobs that would fail on
        ``ssh_connect_kwargs()`` for hosts the operator hasn't finished
        configuring yet.
        """
        return [
            ("ip_address", "!=", False),
            ("user", "!=", False),
            ("known_hosts_key", "!=", False),
            "|",
            "&",
            ("login_type", "=", "ssh_key"),
            ("key_file", "!=", False),
            "&",
            ("login_type", "=", "password"),
            ("password", "!=", False),
        ]

    @api.model
    def cron_collect_metrics(self):
        """Called by ir.cron — queue a host_metrics job for each reachable host."""
        hosts = self.search(self._ssh_ready_domain())
        for host in hosts:
            try:
                self.env["cloud.job"].enqueue(host.id, False, "host_metrics")
            except Exception as e:
                _logger.warning(
                    "Could not enqueue metrics job for host %s: %s",
                    host.name,
                    e,
                )

    @api.model
    def cron_docker_prune(self):
        """Called by ir.cron — queue a docker_prune job for each configured host."""
        hosts = self.search(self._ssh_ready_domain())
        for host in hosts:
            try:
                # docker_prune is a manager-gated job type; the cron bot is
                # not a cloud manager, so enqueue with sudo.
                self.env["cloud.job"].sudo().enqueue(
                    host.id,
                    False,
                    "docker_prune",
                )
            except Exception as e:
                _logger.warning(
                    "Could not enqueue docker_prune job for host %s: %s",
                    host.name,
                    e,
                )

    # ── Traefik template initialization ────────────────────────────────────

    # Image version migrations: old_tag → new_tag applied to stored templates
    _TRAEFIK_IMAGE_MIGRATIONS = [
        ("traefik:2.5", "traefik:v2.11"),
    ]

    # ── Traefik metrics retrofit ───────────────────────────────────────────
    # The Traefik templates are stored PER HOST, and ``init_traefik_templates``
    # only fills EMPTY fields — so editing the seed files in this repo reaches
    # new hosts only. Existing hosts need their stored copy amended in place.
    #
    # The amendment is a minimal merge, never a regeneration: an operator may
    # have hand-edited their template, and rewriting it wholesale would
    # silently discard that. Each helper below is a no-op when the block is
    # already there, so re-running is free.

    _TRAEFIK_METRICS_ENTRYPOINT = '  metrics:\n    address: ":8082"\n'
    _TRAEFIK_METRICS_BLOCK = (
        "\n# Auto-added by IncubaCloud: per-router HTTP metrics.\n"
        "metrics:\n"
        "  prometheus:\n"
        "    entryPoint: metrics\n"
        "    addEntryPointsLabels: true\n"
        "    addRoutersLabels: true\n"
        "    addServicesLabels: true\n"
    )
    _TRAEFIK_METRICS_PORT = '      - "127.0.0.1:8082:8082"\n'

    @staticmethod
    def _add_traefik_metrics(traefik_yml):
        """Return ``traefik.yml`` with the Prometheus wiring, if missing.

        Idempotent and conservative: returns the input untouched when a
        top-level ``metrics:`` key already exists, so a host whose template
        was customised keeps whatever it configured.
        """
        import re

        if not traefik_yml:
            return traefik_yml
        if re.search(r"^metrics:", traefik_yml, re.M):
            return traefik_yml
        out = traefik_yml
        # Add the entryPoint the metrics block refers to, unless present.
        if re.search(r"^entryPoints:", out, re.M) and not re.search(
            r"^\s+metrics:\s*$", out, re.M,
        ):
            out = re.sub(
                r"^(entryPoints:[ \t]*\n)",
                lambda m: m.group(1) + CloudHost._TRAEFIK_METRICS_ENTRYPOINT,
                out,
                count=1,
                flags=re.M,
            )
        return out.rstrip("\n") + "\n" + CloudHost._TRAEFIK_METRICS_BLOCK

    @staticmethod
    def _add_traefik_metrics_port(inverseproxy_yaml):
        """Publish the metrics port on loopback, if not already published.

        Idempotent: keyed on the port mapping itself, so a template that
        already exposes 8082 (however it was written) is left alone.
        """
        import re

        if not inverseproxy_yaml or "8082:8082" in inverseproxy_yaml:
            return inverseproxy_yaml
        if not re.search(r'^\s+-\s+"443:443"\s*$', inverseproxy_yaml, re.M):
            # Unrecognisable ports block — leave it rather than guess.
            return inverseproxy_yaml
        return re.sub(
            r'^(\s+-\s+"443:443"[ \t]*\n)',
            lambda m: m.group(1) + CloudHost._TRAEFIK_METRICS_PORT,
            inverseproxy_yaml,
            count=1,
            flags=re.M,
        )

    @api.model
    def init_traefik_templates(self):
        """
        Populate Traefik config fields for all existing hosts.
        Called from a data XML <function> on module install/upgrade.
        Fills empty fields with the current template and migrates
        outdated Traefik image versions in stored templates.
        """
        config = _read_traefik_template("config.yml")
        inverseproxy = _read_traefik_template("inverseproxy.yaml")
        traefik = _read_traefik_template("traefik.yml")

        hosts = self.search([])
        for host in hosts:
            vals = {}
            if not host.traefik_config_yml:
                vals["traefik_config_yml"] = config
            if not host.traefik_inverseproxy_yaml:
                vals["traefik_inverseproxy_yaml"] = inverseproxy
            if not host.traefik_yml:
                vals["traefik_yml"] = traefik
            # Migrate outdated Traefik image versions in stored templates
            current = (
                vals.get("traefik_inverseproxy_yaml", host.traefik_inverseproxy_yaml)
                or ""
            )
            for old_tag, new_tag in self._TRAEFIK_IMAGE_MIGRATIONS:
                if old_tag in current:
                    current = current.replace(old_tag, new_tag)
                    vals["traefik_inverseproxy_yaml"] = current
                    _logger.info(
                        "Migrated Traefik image %s → %s for host: %s",
                        old_tag,
                        new_tag,
                        host.name,
                    )

            # Retrofit the Prometheus metrics wiring onto stored templates.
            # Per-instance HTTP metrics come from Traefik, and Traefik only
            # emits them when configured — editing the seed files alone
            # would leave every existing host silently unmonitored.
            with_metrics = self._add_traefik_metrics(
                vals.get("traefik_yml", host.traefik_yml),
            )
            if with_metrics != (vals.get("traefik_yml", host.traefik_yml)):
                vals["traefik_yml"] = with_metrics
                _logger.info(
                    "Added Traefik metrics config for host: %s", host.name,
                )
            with_port = self._add_traefik_metrics_port(
                vals.get(
                    "traefik_inverseproxy_yaml",
                    host.traefik_inverseproxy_yaml,
                ),
            )
            if with_port != vals.get(
                "traefik_inverseproxy_yaml", host.traefik_inverseproxy_yaml,
            ):
                vals["traefik_inverseproxy_yaml"] = with_port

            if vals:
                host.write(vals)
                _logger.info("Initialized Traefik templates for host: %s", host.name)
