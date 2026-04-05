"""
Setup Whitelist Executor
------------------------
Creates or updates ~/globalwhitelist/docker-compose.yaml on the remote host
using the host's whitelist entries, then brings the services up (or recreates
changed ones) via docker compose up -d --remove-orphans.

The generated file follows the doodba docker-whitelist pattern: one service
per hostname, each proxying traffic through ghcr.io/tecnativa/docker-whitelist
so that test instances (which have no direct internet access) can still reach
the listed hosts on the shared internal network.

This executor is idempotent: running it again after adding or removing entries
updates the file and restarts only the affected containers.
"""

import re

from .abstract_executor import AbstractSSHExecutor

_WL_TMP = "/tmp/.incubacloud-whitelist.yaml"
_WL_DIR = "~/globalwhitelist"
_WL_PROJECT = "globalwhitelist"
_WL_FILE = f"{_WL_DIR}/docker-compose.yaml"

# Header is a plain string (not f-string) so ${...} shell variables are kept
# verbatim in the generated YAML.
_WL_HEADER = (
    'version: "2.1"\n'
    "\n"
    "networks:\n"
    "  public:\n"
    "    driver_opts:\n"
    "      encrypted: 1\n"
    "  shared:\n"
    "    internal: ${DOODBA_NETWORK_INTERNAL-true}\n"
    "    driver_opts:\n"
    "      encrypted: 1\n"
    "\n"
    "services:\n"
)


def _hostname_to_service(hostname):
    """Convert a hostname to a valid docker-compose service name.

    Replaces every character that is not alphanumeric or underscore with
    an underscore (e.g. 'fonts.googleapis.com' → 'fonts_googleapis_com').
    """
    return re.sub(r"[^a-zA-Z0-9]", "_", hostname)


def build_whitelist_compose(hostnames):
    """Return docker-compose.yaml content for the given hostnames list."""
    content = _WL_HEADER
    for hostname in hostnames:
        svc = _hostname_to_service(hostname)
        content += (
            f"  {svc}:\n"
            f"    image: ghcr.io/tecnativa/docker-whitelist\n"
            f"    restart: unless-stopped\n"
            f"    networks:\n"
            f"      public:\n"
            f"      shared:\n"
            f"        aliases:\n"
            f'          - "{hostname}"\n'
            f"    environment:\n"
            f'      TARGET: "{hostname}"\n'
            f"      PRE_RESOLVE: 1\n"
            f"\n"
        )
    return content


class SetupWhitelistExecutor(AbstractSSHExecutor):
    _job_type = "setup_whitelist"

    def _host(self):
        return self.job.host_id

    def _hostnames(self):
        return self._host().whitelist_ids.mapped("hostname")

    async def before_execute(self, transport):
        hostnames = self._hostnames()
        self._sys(
            f"Preparing whitelist compose file"
            f" ({len(hostnames)} entr{'y' if len(hostnames) == 1 else 'ies'})…"
        )
        content = build_whitelist_compose(hostnames)
        await transport.upload_text_files({_WL_TMP: content})
        self._sys("✓ Whitelist compose file uploaded.")

    def get_commands(self):
        return [
            (
                "Create globalwhitelist directory",
                f"mkdir -p {_WL_DIR}",
            ),
            (
                "Install whitelist compose file",
                f"mv {_WL_TMP} {_WL_FILE}",
            ),
            (
                "Start whitelist services",
                f"cd {_WL_DIR}"
                f" && docker compose -p {_WL_PROJECT}"
                f" -f docker-compose.yaml up -d --remove-orphans",
            ),
        ]

    def parse_results(self, results):
        errors = []
        for label, data in results.items():
            if data.get("exit_status", 1) != 0:
                stderr = (data.get("stderr") or "").strip()
                errors.append(
                    f"'{label}' failed"
                    + (f": {stderr}" if stderr else
                       f" (exit {data.get('exit_status')})")
                )
        return errors

    async def on_success(self, results):
        count = len(self._hostnames())
        self._sys(
            f"✓ Whitelist is up — {count} host"
            f"{'s' if count != 1 else ''} proxied."
        )
        self._resolve_alert("whitelist_setup_failed")

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._alert(
            "whitelist_setup_failed",
            "Whitelist setup failed. Check the job log for details.",
            level="error",
        )
