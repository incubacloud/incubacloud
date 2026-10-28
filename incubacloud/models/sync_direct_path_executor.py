"""Apply the direct list on a host and probe the direct names it publishes.

The list is the platform's addresses (``cloud.host._direct_peers_entries``)
and lives in two places on the host: the nftables set ``ic_direct_v4``
and the ``direct-peers`` middleware of ``~/traefik/dynamic/direct.yml``.
Both are written here and both are read back; a job that cannot confirm
them fails, and nothing is recorded. The proxy is never restarted: its
file provider reloads the document on its own.

Then each direct name the host publishes is probed from the host itself
(``direct_probe.sh``), and the outcome moves the name's state
(``cloud.settings._direct_probe_apply``).
"""
from odoo import _, fields

from . import _direct_probe
from .abstract_executor import AbstractSSHExecutor, JobRefused
from .cloud_host_direct import (
    DIRECT_DYNAMIC_FILE,
    global_ipv4,
    install_direct_yml_command,
)
from .full_setup_executor import _DIRECT_STAGED

#: Where the document is staged (in the job's directory); the same name
#: Full Setup stages it under.
_STAGED = _DIRECT_STAGED
_INSTALL = "Install the direct list for Traefik"
_APPLY = "Apply the direct list in the firewall"
_READ_BACK = "Read back the direct list"


class SyncDirectPathExecutor(AbstractSSHExecutor):
    """Push the direct list to nftables and Traefik, check it, probe the names."""

    _job_type = "sync_direct_path"

    def _host(self):
        """Return the host this job targets."""
        return self.job.host_id

    def pre_run_checks(self):
        """Refuse a host whose proxy publishes no direct port.

        :raises JobRefused: there is nothing to admit anybody to.
        """
        super().pre_run_checks()
        if not self._host().direct_port_shipped:
            message = (
                "This host has no direct port applied: set one and run Full "
                "Setup first."
            )
            self._write_system_direct(f"⚠ {message}")
            raise JobRefused(message)

    async def before_execute(self, transport):
        """Render the list and the document now, and stage the document."""
        host = self._host()
        self._peers = host._direct_peers_entries()
        self._hosts, self._left_out = host._direct_hosts_entries()
        self._document = host._shipped_direct_yml()
        self._names = host._direct_published_names()
        # The probe compares the name's DNS answer with this address: a
        # host registered by name (or a private address) cannot answer it.
        self._address = global_ipv4(host.sudo().ip_address)
        if self._names and not self._address:
            self._sys(
                f"⚠ {host.name} has no global IPv4 address in the panel "
                f"({host.sudo().ip_address}): its direct names are not probed "
                "and stay as they are."
            )
            self._names = []
        await self._upload_files(
            transport, {self._job_path(_STAGED): self._document},
        )
        self._sys(
            f"Direct list for {host.name}: {len(self._peers)} entr"
            f"{'y' if len(self._peers) == 1 else 'ies'}, "
            f"{len(self._names)} direct name(s) to probe."
        )

    def get_commands(self):
        """Install the document, apply the list, read it back, probe."""
        host = self._host()
        port = str(host.direct_port_shipped)
        commands = [
            (
                _INSTALL,
                install_direct_yml_command(self._job_path(_STAGED)),
                {"stop_on_failure": True},
            ),
            (
                _APPLY,
                self.run_script("direct_allowlist.sh", ["set", port, *self._peers]),
                {"stop_on_failure": True},
            ),
            (_READ_BACK, f"cat {DIRECT_DYNAMIC_FILE}"),
        ]
        for name in self._names:
            commands.append((
                f"Probe {name['name']}",
                self.run_script(
                    "direct_probe.sh",
                    [name["name"], self._address, port, *name["checks"]],
                ),
            ))
        return commands

    def parse_results(self, results):
        """Fail unless the host holds exactly what was sent."""
        errors = super().parse_results(results)
        read_back = (results.get(_READ_BACK, {}).get("stdout") or "").strip()
        if read_back != self._document.strip():
            errors.append(
                "The direct list Traefik reads is not the one this job sent."
            )
        applied = _direct_probe.allow_entries(results.get(_APPLY, {}).get("stdout"))
        try:
            same = applied is not None and (
                _direct_probe.covered(applied) == _direct_probe.covered(self._peers)
            )
        except ValueError:
            same = False
        if not same:
            errors.append(
                "The direct list nftables holds is not the one this job sent."
            )
        for name in self._names:
            output = results.get(f"Probe {name['name']}", {}).get("stdout") or ""
            if output.startswith("IC_DIRECT_TOOLS missing"):
                errors.append(
                    f"The host cannot probe {name['name']}: "
                    f"{output.split('missing', 1)[1].strip()} missing."
                )
        return errors

    async def on_success(self, results):
        """Record the list in force, say what moved, and feed the probes."""
        host = self._host()
        before = (host.direct_allowlist_shipped or "").split()
        host.write({
            "direct_allowlist_shipped": "\n".join(self._peers),
            "direct_allowlist_synced_at": fields.Datetime.now(),
            "direct_allowlist_stale_since": False,
        })
        self.env["cloud.alert"].sudo().resolve_alert(
            "direct_allowlist_stale", host=host,
        )
        entered = [e for e in self._peers if e not in before]
        left = [e for e in before if e not in self._peers]
        summary = (
            f"✓ {len(self._peers)} entries admitted on port "
            f"{host.direct_port_shipped}"
        )
        if entered:
            summary += f"; in: {', '.join(entered)}"
        if left:
            summary += f"; out: {', '.join(left)}"
        self._sys(summary + ".")
        if self._left_out:
            self._sys(
                "Left out although active (absent from the metrics over a day, "
                f"or their server is gone): {', '.join(self._left_out)}."
            )
        settings = self.env["cloud.settings"].sudo()._get_system()
        for name in self._names:
            outcome = _direct_probe.parse_probe(
                results.get(f"Probe {name['name']}", {}).get("stdout"),
            )
            old, new = settings._direct_probe_apply(name["prefix"], outcome)
            verdict = (
                "answers" if outcome["ok"]
                else f"fails ({outcome['layer']}: {outcome['detail']})"
            )
            self._sys(f"{name['name']} {verdict}; state {old} → {new}.")
        if before != self._peers:
            self.env["cloud.host"]._direct_path_changed(
                f"direct list applied on {host.name}",
            )

    async def on_failure(self, results, errors):
        """Say what failed; the cron tries again and alerts if it lasts."""
        for err in errors:
            self._sys(f"✗ {err}")
        self._sys(_("The previous direct list stays in force on this host."))
