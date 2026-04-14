"""
Setup Traefik Executor
----------------------
Deploys the Traefik reverse proxy on the remote host using the host's
configured Traefik files.

Steps:
  1. Upload traefik.yml, config.yml and inverseproxy.yaml to ~/traefik/
     - inverseproxy.yaml has domain and password hash substituted
  2. docker compose -p inverseproxy -f inverseproxy.yaml up -d

The password hash is generated locally with passlib bcrypt (compatible with
htpasswd -nB) and dollar signs are doubled for docker-compose label escaping.
bcrypt enforces a 72-byte limit; the password is truncated explicitly so
behaviour is consistent across bcrypt versions (>= 4.0 raises instead of
silently truncating).
"""

import re

from passlib.hash import bcrypt as _bcrypt

from .abstract_executor import AbstractSSHExecutor

_TMP = "/tmp/.incubacloud-traefik"


def _htpasswd_hash(password):
    """Return a docker-compose-safe bcrypt hash for traefik-admin:<password>."""
    # bcrypt >= 4.0 raises on passwords > 72 bytes instead of truncating.
    # Encode to UTF-8, truncate, then decode back for passlib.
    pw = (password or "").encode("utf-8")[:72].decode("utf-8", "ignore")
    hashed = _bcrypt.using(rounds=12).hash(pw)
    # $2b$ → $2y$: equivalent variants; htpasswd/Traefik expect $2y$.
    # Double $ for docker-compose label escaping.
    return "traefik-admin:" + hashed.replace("$2b$", "$2y$", 1).replace("$", "$$")


def _build_inverseproxy(content, wildcard_domain, panel_password):
    """Substitute domain and password hash in inverseproxy.yaml content."""
    traefik_domain = (
        f"traefik.{wildcard_domain}"
        if wildcard_domain else "traefik.localhost"
    )
    # Replace the Host() rule
    content = re.sub(
        r'Host\(`[^`]+`\)',
        f"Host(`{traefik_domain}`)",
        content,
    )
    # Replace the basicauth.users label value
    if panel_password:
        label_value = _htpasswd_hash(panel_password)
        content = re.sub(
            r'traefik\.http\.middlewares\.auth\.basicauth\.users=[^\n"]+',
            f"traefik.http.middlewares.auth.basicauth.users={label_value}",
            content,
        )
    return content


class SetupTraefikExecutor(AbstractSSHExecutor):
    _job_type = "setup_traefik"

    def _host(self):
        return self.job.host_id

    async def before_execute(self, transport):
        host = self._host()
        self._sys("Preparing Traefik configuration files...")

        inverseproxy = _build_inverseproxy(
            host.traefik_inverseproxy_yaml or "",
            host.wildcard_domain,
            host.traefik_panel_password,
        )

        files = {
            f"{_TMP}-inverseproxy.yaml": inverseproxy,
            f"{_TMP}-traefik.yml": host.traefik_yml or "",
            f"{_TMP}-config.yml": host.traefik_config_yml or "",
        }

        await transport.upload_text_files(files)
        self._sys("✓ Configuration files uploaded.")

    def get_commands(self):
        return [
            (
                "Create traefik directory",
                "mkdir -p ~/traefik",
            ),
            (
                "Move config files",
                f"mv {_TMP}-inverseproxy.yaml ~/traefik/inverseproxy.yaml"
                f" && mv {_TMP}-traefik.yml ~/traefik/traefik.yml"
                f" && mv {_TMP}-config.yml ~/traefik/config.yml",
            ),
            (
                "Start Traefik",
                "cd ~/traefik && docker compose"
                " -p inverseproxy -f inverseproxy.yaml up -d",
            ),
        ]

    def parse_results(self, results):
        return [
            f"'{label}' failed with exit code {data.get('exit_status')}"
            for label, data in results.items()
            if data.get("exit_status", 1) != 0
        ]

    async def on_success(self, results):
        self._sys("✓ Traefik is up and running.")
        self._resolve_alert('traefik_setup_failed')
        self.job.host_id.write({'traefik_deployed': True})

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._alert(
            "traefik_setup_failed",
            "Traefik setup failed. Check the job log for details.",
            level="error",
        )
