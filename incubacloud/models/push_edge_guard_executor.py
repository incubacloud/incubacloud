"""Write the edge guard to a host that is already set up.

The document (``cloud.host._shipped_edge_guard_yml``) refuses Odoo's
``/website/info`` at the host's proxy. ``scripts/edge_guard.sh`` writes it
only when it changed, checks the proxy reads the directory and asks the
proxy for the page. The proxy is never restarted: its file provider
reloads the document on its own.

Exit 3 (the proxy does not mount the directory) and 4 (the handshake was
refused, so the answer could not be verified) are not failures: the file is
written either way. The first raises a warning on the host until a run
finds it mounted; the second is only said in the log, because a host that
requires the CDN's client certificate can never be verified from inside.
Exit 5 is a failure: the proxy answered with a real HTTP status other than
403, so the guard is not applied (Traefik dropped the file, say).
"""
from odoo import fields

from .abstract_executor import AbstractSSHExecutor, JobRefused
from .cloud_host_edge_guard import EDGE_GUARD_STAGED, edge_guard_digest

#: The one step's label.
_INSTALL = "Install the edge guard"

#: The script's "written, but the proxy does not read the directory".
EDGE_GUARD_NOT_MOUNTED = 3

#: The script's "written, the proxy's answer could not be verified".
EDGE_GUARD_UNVERIFIED = 4

#: The script's "the proxy answered, and not with 403: the guard is not applied".
EDGE_GUARD_NOT_APPLIED = 5

#: The alert a host whose proxy does not read the guard carries.
EDGE_GUARD_ALERT = "edge_guard_not_mounted"


class PushEdgeGuardExecutor(AbstractSSHExecutor):
    """Install ``~/traefik/dynamic/ic_guard.yml`` and see whether it holds."""

    _job_type = "push_edge_guard"

    def _host(self):
        """Return the host this job targets."""
        return self.job.host_id

    def pre_run_checks(self):
        """Refuse a host without the panel's proxy, or one left alone.

        :raises JobRefused: there is no proxy to guard, or this host's
            proxy is not the panel's to change.
        """
        super().pre_run_checks()
        host = self._host()
        if not host.traefik_deployed:
            message = (
                "This host's proxy has not been set up yet: Full Setup "
                "writes the edge guard."
            )
        elif host._edge_guard_leaves_alone():
            message = "The edge guard is not written to this host."
        else:
            return
        self._write_system_direct(f"⚠ {message}")
        raise JobRefused(message)

    async def before_execute(self, transport):
        """Render the guard now and stage it in the job's directory."""
        self._document = self._host()._shipped_edge_guard_yml()
        await self._upload_files(
            transport, {self._job_path(EDGE_GUARD_STAGED): self._document},
        )
        self._sys("✓ Edge guard prepared.")

    def get_commands(self):
        """Install the guard, check the mount, probe the page."""
        return [(
            _INSTALL,
            self.run_script(
                "edge_guard.sh", ["install", self._job_path(EDGE_GUARD_STAGED)],
            ),
            {"continue_on": (EDGE_GUARD_NOT_MOUNTED, EDGE_GUARD_UNVERIFIED)},
        )]

    def _status(self, results):
        """Return the install step's exit status (1 when it did not run)."""
        return results.get(_INSTALL, {}).get("exit_status", 1)

    def _answer(self, results):
        """Return the HTTP code the script reported with exit 5, or ``?``."""
        stdout = results.get(_INSTALL, {}).get("stdout") or ""
        for line in stdout.splitlines():
            if line.startswith("IC_EDGE_GUARD_NOT_APPLIED "):
                return line.split(None, 1)[1].strip()
        return "?"

    def parse_results(self, results):
        """0, 3 and 4 wrote the guard; 5 and anything else did not."""
        status = self._status(results)
        if status == EDGE_GUARD_NOT_MOUNTED:
            self._sys(
                "⚠ The edge guard is written, but this host's proxy does not "
                "read its dynamic directory: it applies after the next Full "
                "Setup."
            )
        elif status == EDGE_GUARD_UNVERIFIED:
            self._sys(
                "⚠ The edge guard is written, but the proxy's answer could "
                "not be verified from the host itself."
            )
        elif status == EDGE_GUARD_NOT_APPLIED:
            answer = self._answer(results)
            return [
                (
                    f"The proxy answered {answer} for /website/info instead "
                    "of 403: the edge guard is not applied."
                ),
            ]
        elif status != 0:
            return [f"'{_INSTALL}' exited with status {status}."]
        return []

    async def on_success(self, results):
        """Record what the proxy reads, and raise or clear the mount alert."""
        host = self._host()
        if self._status(results) == EDGE_GUARD_NOT_MOUNTED:
            self._alert(
                EDGE_GUARD_ALERT,
                f"The proxy on {host.name} does not read its dynamic "
                "directory, so the edge guard that refuses Odoo's "
                "/website/info is not applied. Run Full Setup on the host.",
                level="warning",
            )
            return
        # 0 and 4 both found the directory mounted.
        host.write({
            "edge_guard_shipped_hash": edge_guard_digest(self._document),
            "edge_guard_shipped_at": fields.Datetime.now(),
        })
        self._resolve_alert(EDGE_GUARD_ALERT)
        self._sys("✓ Edge guard applied.")

    async def on_failure(self, results, errors):
        """Report each error line to the job's system log."""
        for err in errors:
            self._sys(f"✗ {err}")
