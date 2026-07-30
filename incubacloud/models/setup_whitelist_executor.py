"""
Setup Whitelist Executor
------------------------
Creates or updates ~/globalwhitelist/docker-compose.yaml on the remote
host from the host's whitelist entries, then converges the services, via
``ansible/playbooks/host_whitelist.yml``.

The generated file follows the doodba docker-whitelist pattern: one
service per hostname, each proxying traffic through
ghcr.io/tecnativa/docker-whitelist so that test instances (which have no
direct internet access) can still reach the listed hosts on the shared
internal network.

Idempotent: ``docker compose up -d --remove-orphans`` recreates only the
services whose definition changed and drops the ones removed from the
list.

The compose builder and the ``_WL_*`` constants are the module's public
surface — ``full_setup_executor`` imports them to deploy the same stack
inline as part of a full host setup.
"""

import re

from .ansible_executor import AnsibleExecutor

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
            f'  "{svc}":\n'
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


class SetupWhitelistExecutor(AnsibleExecutor):
    _job_type = "setup_whitelist"
    _playbook = "playbooks/host_whitelist.yml"

    def _host(self):
        return self.job.host_id

    def _hostnames(self):
        return self._host().whitelist_ids.mapped("hostname")

    def get_extra_vars(self):
        """Hand the generated compose file to the playbook.

        The file is built here, from the host's whitelist entries, so the
        playbook stays a pure "write it and converge" step.
        """
        hostnames = self._hostnames()
        self._sys(
            f"Preparing whitelist compose file"
            f" ({len(hostnames)} entr{'y' if len(hostnames) == 1 else 'ies'})…"
        )
        return {"ic_whitelist_compose": build_whitelist_compose(hostnames)}

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
            level="critical",
        )
