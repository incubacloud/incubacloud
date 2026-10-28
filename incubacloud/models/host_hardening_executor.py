"""Host hardening — layer-1 in-host security recipe (core).

Repatriated to core per the architecture decision (2026-07-17): the
in-host hardening recipe is generic and belongs in the distributable
core, while the network-edge firewall (layer 2 — Hetzner Cloud Firewall,
etc.) follows the provider and stays in the SaaS layer. The two SaaS
modules that previously each carried a full copy of this recipe now keep
only a thin subclass that overrides the edge-firewall hook.

The recipe itself lives in ``ansible/playbooks/host_hardening.yml``
(machine-state convergence via Ansible). This executor supplies the
per-host values (rotated SSH port, operator user, allowlist, reboot
policy) as extra-vars and performs the one step a playbook cannot do
safely from inside its own connection: disable root login only AFTER
proving the hardened user can log in on the new port.

Lock-out safety: the playbook deliberately leaves
``PermitRootLogin prohibit-password`` — key-based root still works — so a
failed run never locks the operator out. ``on_success`` opens the edge
firewall on the new port (a hook; no-op in core), connects as the
hardened user on the new port, and only then flips root to ``no``. If
that connection fails, root is left reachable and the job fails.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timezone

import asyncssh

from odoo.tools import file_path

from .ansible_executor import AnsibleExecutor
from .cloud_host_posture import HARDENING_OUTDATED_ALERT_CODE

_logger = logging.getLogger(__name__)

# Default operator user created when starting from root. Hardcoded by
# design — tooling, scripts and audit-log filters rely on the well-known
# name.
DEFAULT_HARDENED_USER = "incubacloud"

_SSH_PORT_MIN = 22000
_SSH_PORT_MAX = 29999

# Path of the sshd drop-in the playbook writes; the final root-disable
# step edits this same file.
_SSHD_DROPIN = "/etc/ssh/sshd_config.d/00-incubacloud-hardening.conf"


def _pick_new_ssh_port(current_port):
    """Return the current port if already non-default, else a random one."""
    if current_port and current_port != 22:
        return current_port
    return secrets.choice(range(_SSH_PORT_MIN, _SSH_PORT_MAX + 1))


def _recipe_fingerprint_of(text):
    """Return the fingerprint of a hardening recipe's text.

    Full-line comments -- the playbook's own and those of the files it
    writes on the host -- and blank lines are left out: the recipe
    explains itself at length, and rewording an explanation must not
    make every hardened host read as behind. Everything else counts.

    :param str text: the playbook's content
    :return: the first 12 hex digits of its SHA-256
    :rtype: str
    """
    lines = [
        line.rstrip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def _build_ssh_allowlist(primary_ip, extra_ips):
    """Build the nftables set of source IPs allowed to reach SSH.

    Falls back to ``0.0.0.0/0`` when nothing is known, so a missing
    operator IP degrades to "reachable" rather than locking everyone out.
    """
    parts = []
    if primary_ip:
        parts.append(primary_ip.strip())
    for raw in (extra_ips or "").split(","):
        ip = raw.strip()
        if ip and ip not in parts:
            parts.append(ip)
    if not parts:
        parts.append("0.0.0.0/0")
    return ", ".join(parts)


class HostHardeningExecutor(AnsibleExecutor):
    """Apply the in-host hardening recipe and finish by disabling root."""

    _job_type = "host_hardening"
    _playbook = "playbooks/host_hardening.yml"
    # The alert a failed run files with what failed in it; while it is
    # open, ``cloud.job._create_job_failed_alert`` raises no
    # ``job_failed`` for the same job, which told the same failure twice
    # (job 382128, 27-oct-2026).
    _failure_alert_code = "hardening_failed"

    @classmethod
    def _recipe_fingerprint(cls):
        """Return the fingerprint of the recipe this executor applies.

        Read from the playbook a run would use: that of the most derived
        addon shipping it, as ``_addon_overlay`` resolves it.

        :return: see :func:`_recipe_fingerprint_of`; ``""`` when no addon
            ships the playbook
        :rtype: str
        """
        for addon in cls._addon_chain():
            try:
                path = file_path(f"{addon}/ansible/{cls._playbook}")
            except FileNotFoundError:
                continue
            with open(path, encoding="utf-8") as fh:
                return _recipe_fingerprint_of(fh.read())
        return ""

    def _host(self):
        """Return the host record this job targets."""
        return self.job.host_id

    def _prepare(self):
        """Resolve the rotated port, operator user and allowlist once.

        ``AnsibleExecutor`` does not call ``before_execute``, so the setup
        the old SSH executor did there happens here — invoked from
        ``get_extra_vars`` (before the playbook) and cached for
        ``on_success`` (after it).
        """
        if getattr(self, "_prepared", False):
            return
        host = self._host()
        self._is_first_run = not host.hardened
        self._new_port = _pick_new_ssh_port(host.port)

        # Keep the existing non-root user when the host is BYOH (cloud
        # images ship ubuntu / ec2-user / azureuser…); only migrate
        # root → the default operator when starting fresh.
        connecting_user = (host.user or "").strip() or "root"
        if connecting_user == "root":
            self._hardened_user = DEFAULT_HARDENED_USER
            self._needs_user_creation = True
        else:
            self._hardened_user = connecting_user
            self._needs_user_creation = False

        self._ssh_allowlist = _build_ssh_allowlist(
            self._operator_ip(), host.allowed_ssh_ips,
        )
        # Resolved once, like the rest: what the playbook is handed is
        # what ``on_success`` records as applied, even if the flags move
        # while the run is under way.
        self._http_allowlist = self._http_allowlist_vars()
        self._prepared = True

    def _operator_ip(self):
        """Return the primary IP to seed the allowlist with: the panel's own.

        The host says it (``cloud.host._hardening_panel_ip``), and the
        hardening dialog reads the same answer to tell who will be able to
        open SSH. Core knows no address of its own, so it is ``''`` there
        and the allowlist is built entirely from ``allowed_ssh_ips`` —
        open to any address when that is empty; the layers that know where
        the panel connects from (the SaaS manager's ``core_public_ip``, a
        tenant panel's ``tenant_public_ip``) answer on the host.
        """
        return self._host()._hardening_panel_ip()

    def get_extra_vars(self):
        """Hand the per-host hardening values to the playbook."""
        self._prepare()
        host = self._host()
        self._sys(
            f"Hardening {host.name} ({host.ip_address}). "
            f"First run: {self._is_first_run}. "
            f"Operator user: {self._hardened_user} "
            f"({'create' if self._needs_user_creation else 'reuse'}). "
            f"SSH port: {host.port} → {self._new_port}. "
            f"Auto reboot: {host.auto_security_updates}."
        )
        return {
            "ic_hardened_user": self._hardened_user,
            "ic_ssh_port": self._new_port,
            "ic_ssh_allowlist": self._ssh_allowlist,
            "ic_auto_reboot": bool(host.auto_security_updates),
            # The per-source cap counts new connections by the address
            # that opened them. Behind a CDN every visitor arrives from
            # the same handful of edge addresses, so the cap would throttle
            # the CDN itself and protect nobody -- whether or not the host
            # also refuses direct traffic. A host still open at the origin
            # while behind the CDN (the state a pool host is in between
            # moving its names and closing its firewall) therefore gets no
            # cap at all, exactly as before the cap existed. The rate is
            # kept on the host record: it comes back the day the host
            # leaves the CDN.
            "ic_http_conn_rate": 0 if host.behind_cdn else (host.http_conn_rate or 0),
            # The web-login jail bans by the address that opened the
            # connection. Behind a CDN that address is the CDN's, and
            # banning it would cut every visitor that edge serves.
            "ic_behind_cdn": bool(host.behind_cdn),
            # Addresses the proxy is told to trust as proxies are never
            # visitors, so the web-login jail must not ban them either:
            # a failed login arriving through one is the visitor's, not
            # the proxy's.
            "ic_trusted_proxy_ranges": " ".join(host._effective_trusted_proxy_ranges()),
            # Filtering 80/443 to the CDN's ranges replaces the per-source
            # rate cap; both at once would count the edge as one visitor.
            # Deliberately gated on the host also refusing direct access
            # at the proxy: enabling the firewall half on its own is how
            # a host stops answering anybody without the panel saying so.
            **self._http_allowlist,
            # The direct port's set and rules, with the list the host has
            # in force, so hardening again does not lose them.
            **self._direct_vars(),
        }

    def _direct_vars(self):
        """Return the firewall variables of the host's direct port, if any.

        The port the last Full Setup published and the list the last sync
        applied; a host whose list was never applied gets the one it
        should have.

        :rtype: dict
        """
        host = self.job.host_id
        port = host.direct_port_shipped or 0
        if not port:
            return {}
        entries = (host.direct_allowlist_shipped or "").split()
        if not entries:
            entries = host._direct_peers_entries()
        return {"ic_direct_port": port, "ic_direct_allowlist": ", ".join(entries)}

    def _http_allowlist_vars(self):
        """Return the firewall variables for a host behind a CDN.

        Empty — the pre-existing behaviour, 80/443 open to the world —
        unless the host says its hardening admits only its proxies
        (``cloud.host._hardening_admits_only_proxies``, which the posture
        check asks too). Split by address family because nftables needs
        ``ip saddr`` and ``ip6 saddr`` in separate rules.

        :rtype: dict
        """
        host = self.job.host_id
        if not host._hardening_admits_only_proxies():
            return {}
        ranges = host._effective_trusted_proxy_ranges()
        v4 = [r for r in ranges if ":" not in r]
        v6 = [r for r in ranges if ":" in r]
        return {
            "ic_http_allowed_ranges": ", ".join(ranges),
            "ic_http_allowed_ranges_v4": ", ".join(v4),
            "ic_http_allowed_ranges_v6": ", ".join(v6),
        }

    def parse_results(self, results):
        """Confirm the playbook proved the new SSH port came up."""
        errors = []
        facts = self.playbook_facts()
        rc = results.get(self._playbook, {}).get("exit_status", 1)
        if rc != 0:
            errors.append(
                f"Hardening playbook failed (rc={rc}) — see the log."
            )
            return errors
        port_listening = str(facts.get("ic_port_listening", "")).lower()
        if port_listening not in ("true", "1"):
            errors.append(
                f"sshd is not listening on the new port {self._new_port} "
                f"after the playbook — aborting before disabling root SSH."
            )
        return errors

    async def on_success(self, results):
        """Open the edge firewall, prove the new login, then disable root."""
        self._prepare()
        host = self._host()

        # Layer 2: open SSH on the rotated port at the network edge. No-op
        # in core; SaaS subclasses override for Hetzner / RPC-to-core.
        await self._open_edge_firewall_port(self._new_port)

        if not await self._finalize_disable_root():
            self._alert(
                "hardening_failed",
                f"Hardening aborted: could not log in as "
                f"{self._hardened_user} on port {self._new_port} after "
                f"applying the recipe. Root SSH was preserved. Check "
                f"allowed_ssh_ips and any edge firewall, then retry.",
                level="critical",
            )
            raise RuntimeError(
                f"Hardening aborted: SSH as {self._hardened_user} on port "
                f"{self._new_port} failed; root SSH preserved."
            )

        vals = {
            "hardened": True,
            "hardened_at": datetime.now(timezone.utc).replace(tzinfo=None),
            # What this run applied, which the hourly posture check
            # compares with what the panel wants now: the recipe, and
            # whether 80/443 were closed to all but the trusted proxies.
            "hardening_recipe": self._recipe_fingerprint(),
            "hardening_cdn_only": bool(self._http_allowlist),
        }
        if self._is_first_run:
            vals["port"] = self._new_port
            if self._needs_user_creation:
                vals["user"] = self._hardened_user
        # Hardening rotates the SSH port from inside its own running job;
        # the endpoint-change guard would self-block. Opt out.
        host.with_context(skip_endpoint_change_guard=True).write(vals)
        # Same machine, new port — so the verified key stays and is simply
        # re-filed under the rotated endpoint. Skipping this leaves the
        # entry labelled for the old port, which asyncssh tolerates but
        # OpenSSH (every Ansible-backed job) reads as "host unknown".
        host._relabel_known_hosts_entry()
        self._resolve_alert("hardening_failed")
        self._resolve_alert(HARDENING_OUTDATED_ALERT_CODE)
        self._sys(
            f"✓ Host hardened. Reachable as {self._hardened_user} on port "
            f"{self._new_port}; root SSH disabled."
        )

    async def on_failure(self, results, errors):
        """Report hardening failure, with what failed; root SSH remains reachable.

        The one alert of the failure: it says what failed, which only
        ``job_failed`` used to, and that one is no longer raised beside
        it (``_failure_alert_code``).
        """
        for err in errors:
            self._sys(f"✗ {err}")
        detail = " ".join(errors) or "Check the job log for details."
        self._alert(
            self._failure_alert_code,
            f"Hardening {self._host().name} failed: {detail} "
            f"Root SSH was preserved so you can recover; re-run to retry.",
            level="critical",
        )

    # ── Layer-2 edge firewall hook (overridden by SaaS) ───────────────────

    async def _open_edge_firewall_port(self, new_port):
        """Open SSH on *new_port* at the network edge firewall.

        No-op in core: a self-hosted / BYOH host has no provider-managed
        edge firewall. SaaS modules override this to update the Hetzner
        Cloud Firewall (manager) or RPC core to do it (tenant), so the
        new-port login test below is not dropped by a stale allowlist.
        """
        return None

    # ── New-login proof + root disable ────────────────────────────────────

    async def _finalize_disable_root(self):
        """Connect as the hardened user on the new port, then disable root.

        Returns True when the login succeeds and root has been disabled;
        False on any failure (root is then left as the playbook set it —
        ``prohibit-password`` — so the operator can still recover).
        """
        host = self._host()
        # Trusted reader, same pattern as ``get_transport``: the job was
        # authorized at enqueue and the credential fields are gated for
        # raw reads. Masked today (hardening is manager-mapped) but the
        # elevation must not depend on the type's floor never changing.
        connect_kw = dict(host.sudo().ssh_connect_kwargs())
        connect_kw["username"] = self._hardened_user
        connect_kw["port"] = self._new_port
        # The host key is unchanged but the port rotated; skip the pin for
        # this internal proof connection only.
        connect_kw["known_hosts"] = None
        try:
            async with asyncssh.connect(**connect_kw) as conn:
                idr = await conn.run("id", check=True)
                if self._hardened_user not in (idr.stdout or ""):
                    return False
                # New access proven — now flip root off and reload sshd.
                # Under socket activation sshd may be idle (ssh.service not
                # running because ssh.socket holds the port), and reloading
                # a stopped unit fails; there is nothing to reload in that
                # case because the next connection spawns sshd fresh and
                # reads the new config. Reload only when it is running.
                await conn.run(
                    "sudo sed -i 's/^PermitRootLogin .*/PermitRootLogin no/' "
                    f"{_SSHD_DROPIN} && "
                    "if systemctl is-active --quiet ssh.service; then "
                    "sudo systemctl reload ssh.service; fi",
                    check=True,
                )
                return True
        except Exception as exc:
            _logger.warning(
                "host_hardening: finalize (login as %s on port %s) failed "
                "for %s: %s",
                self._hardened_user, self._new_port, host.name, exc,
            )
            return False
