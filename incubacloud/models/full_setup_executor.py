"""
Full Setup Executor
-------------------
Single-pass setup for a new host (or re-run to repair):

  Phase 1 — Compatibility check: OS (Linux) + available disk (≥ 10 GB)
  Phase 2 — Tool installation: git, Docker CE, Compose V2, pipx,
             copier, invoke, pre-commit  (all idempotent)
  Phase 3 — Traefik deployment: upload config files + docker compose up -d
  Phase 4 — Whitelist: create/update globalwhitelist services

Status transitions:
  Job enqueued  → checking      (set at job start, before SSH)
  All phases OK → compatible    (traefik_deployed=True)
  Any failure   → unsupported

"degraded" is reserved exclusively for the host_metrics executor
(disk/CPU/RAM resource monitoring).

This executor is the single entry point for host setup. The older
per-phase executors (``setup_host``, ``setup_traefik``) have been
retired — their logic (SSH commands + Traefik templating) now lives
inline below so there's only one codepath for inheriting modules to
wrap.
"""

import re

import bcrypt as _bcrypt

from .abstract_executor import AbstractSSHExecutor
from .setup_whitelist_executor import (
    build_whitelist_compose,
    _WL_TMP, _WL_DIR, _WL_FILE, _WL_PROJECT,
)

MIN_DISK_GB = 10
_TMP = "/tmp/.incubacloud-traefik"
_BIN = 'PATH="$HOME/.local/bin:$PATH"'


# ── SSH command catalog (Phase 2) ─────────────────────────────────────
# Each tuple: (label shown in logs, shell command). Every step is
# idempotent so re-running the job only installs what's missing.
SETUP_COMMANDS = [
    # ── System update ──────────────────────────────────────────────────
    (
        "Update package index",
        "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq",
    ),
    # ── Git ────────────────────────────────────────────────────────────
    (
        "Install git",
        "command -v git >/dev/null 2>&1 "
        "|| sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git",
    ),
    # Seed init.defaultBranch=master once per host so concurrent deploys
    # don't race for the ~/.gitconfig lock. Idempotent: only writes when
    # the setting is absent. Setup is serialized (single full_setup job
    # per host), so no contention here.
    (
        "Set git default branch",
        "git config --global --get init.defaultBranch >/dev/null 2>&1"
        " || git config --global init.defaultBranch master",
    ),
    # ── Docker CE ──────────────────────────────────────────────────────
    (
        "Install Docker",
        "command -v docker >/dev/null 2>&1 "
        "|| (curl -fsSL https://get.docker.com | sudo sh)",
    ),
    (
        "Add user to docker group",
        "groups | grep -q docker "
        "|| (sudo usermod -aG docker \"$USER\" && echo 'Added to docker group')",
    ),
    # ── Docker Compose V2 ──────────────────────────────────────────────
    (
        "Install Docker Compose plugin",
        "docker compose version >/dev/null 2>&1 "
        "|| sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-plugin",
    ),
    # ── Python toolchain ───────────────────────────────────────────────
    (
        "Install python3-pip and python3-venv",
        "dpkg -s python3-pip python3-venv >/dev/null 2>&1 "
        "|| sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "python3-pip python3-venv",
    ),
    (
        "Install pipx",
        "command -v pipx >/dev/null 2>&1 "
        "|| python3 -m pip install --break-system-packages --user pipx 2>/dev/null "
        "|| python3 -m pip install --user pipx",
    ),
    (
        "Add ~/.local/bin to PATH",
        "grep -q '.local/bin' ~/.bashrc "
        "|| echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc",
    ),
    # ── Deployment tools ───────────────────────────────────────────────
    (
        "Install copier",
        f"{_BIN} pipx install copier 2>/dev/null || {_BIN} pipx upgrade copier",
    ),
    (
        "Install invoke",
        f"{_BIN} pipx install invoke 2>/dev/null || {_BIN} pipx upgrade invoke",
    ),
    (
        "Install pre-commit",
        f"{_BIN} pipx install pre-commit 2>/dev/null || {_BIN} pipx upgrade pre-commit",
    ),
    # ── Zip ────────────────────────────────────────────────────────────
    (
        "Install zip",
        "command -v zip >/dev/null 2>&1 || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zip",
    ),
    # ── Verification ───────────────────────────────────────────────────
    ("Verify git",        "git --version 2>&1 || echo FAILED"),
    ("Verify docker",     "docker --version 2>&1 || echo FAILED"),
    ("Verify compose",    "docker compose version 2>&1 || echo FAILED"),
    ("Verify copier",     f'{_BIN} copier --version 2>&1 || echo FAILED'),
    ("Verify invoke",     f'{_BIN} invoke --version 2>&1 || echo FAILED'),
    ("Verify pre-commit", f'{_BIN} pre-commit --version 2>&1 || echo FAILED'),
    ("Verify zip",        "zip --version 2>&1 || echo FAILED"),
]

_VERIFY_LABELS = (
    "Verify git",
    "Verify docker",
    "Verify compose",
    "Verify copier",
    "Verify invoke",
    "Verify pre-commit",
    "Verify zip",
)


# ── Traefik template helpers (Phase 3) ────────────────────────────────
# The panel password hash is compatible with ``htpasswd -nB`` (Traefik's
# basicauth consumes that format). We call ``bcrypt`` directly instead
# of going through passlib — passlib 1.7.4 doesn't know how to talk to
# bcrypt >= 5.0 (its backend-probe reads ``bcrypt.__about__`` which is
# gone) and falls back to a path that still raises on 72-byte inputs.
# Dollar signs are doubled for docker-compose label escaping.

def _htpasswd_hash(password):
    """Compose-safe bcrypt hash for ``traefik-admin:<password>``."""
    # bcrypt has a hard 72-byte input limit — truncate deterministically
    # on bytes (never on characters, which miscounts multi-byte utf-8).
    pw_bytes = (password or "").encode("utf-8")[:72]
    salt = _bcrypt.gensalt(rounds=12)
    # $2b$ → $2y$: equivalent variants; htpasswd/Traefik expect $2y$.
    # Double $ for docker-compose label escaping.
    return "traefik-admin:" + (
        _bcrypt.hashpw(pw_bytes, salt)
        .decode("ascii")
        .replace("$2b$", "$2y$", 1)
        .replace("$", "$$")
    )


def _build_inverseproxy(content, wildcard_domain, panel_password):
    """Substitute domain and password hash in inverseproxy.yaml content."""
    traefik_domain = (
        f"traefik.{wildcard_domain.removeprefix('*.')}"
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


class FullSetupExecutor(AbstractSSHExecutor):
    _job_type = "full_setup"

    def _host(self):
        return self.job.host_id

    async def before_execute(self, transport):
        # Mark host as 'checking' so the dashboard updates immediately
        with self.job.env.registry.cursor() as cr:
            self.job.env(cr=cr)['cloud.host'].browse(
                self._host().id
            ).write({'status': 'checking'})

        self._sys("Phase 1/4 — Checking host compatibility...")

        host = self._host()

        # Upload Traefik config files (Phase 3)
        self._sys("Preparing Traefik configuration files...")
        inverseproxy = _build_inverseproxy(
            host.traefik_inverseproxy_yaml or "",
            host.wildcard_domain,
            host.traefik_panel_password,
        )
        await transport.upload_text_files({
            f"{_TMP}-inverseproxy.yaml": inverseproxy,
            f"{_TMP}-traefik.yml":       host.traefik_yml or "",
            f"{_TMP}-config.yml":        host.traefik_config_yml or "",
        })
        self._sys("✓ Traefik configuration files uploaded.")

        # Upload whitelist compose file (Phase 4)
        hostnames = host.whitelist_ids.mapped("hostname")
        n = len(hostnames)
        self._sys(
            f"Preparing whitelist compose file"
            f" ({n} entr{'y' if n == 1 else 'ies'})…"
        )
        await transport.upload_text_files(
            {_WL_TMP: build_whitelist_compose(hostnames)},
        )
        self._sys("✓ Whitelist compose file uploaded.")

    def get_commands(self):
        return [
            # ── Phase 1: compatibility ────────────────────────────────
            ("check:os",   "uname -s"),
            ("check:disk", "df -B1 --output=avail / | tail -n 1"),
        ] + SETUP_COMMANDS + [
            # ── Phase 3: Traefik ──────────────────────────────────────
            (
                "Create traefik directory",
                "mkdir -p ~/traefik",
            ),
            (
                "Move traefik config files",
                f"mv {_TMP}-inverseproxy.yaml"
                " ~/traefik/inverseproxy.yaml"
                f" && mv {_TMP}-traefik.yml ~/traefik/traefik.yml"
                f" && mv {_TMP}-config.yml ~/traefik/config.yml",
            ),
            (
                "Start Traefik",
                "cd ~/traefik && docker compose"
                " -p inverseproxy -f inverseproxy.yaml up -d",
            ),
            # ── Phase 4: Whitelist ────────────────────────────────────
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
                f"cd {_WL_DIR} && docker compose"
                f" -p {_WL_PROJECT} -f docker-compose.yaml"
                " up -d --remove-orphans",
            ),
        ]

    def parse_results(self, results):
        errors = []

        # ── Phase 1: OS ───────────────────────────────────────────────
        os_out = (
            results.get("check:os", {}).get('stdout', '').strip()
        )
        if os_out.lower() == 'linux':
            self._sys(f"✓ OS: {os_out}")
        else:
            errors.append(
                f"Unsupported OS: '{os_out or 'unknown'}'"
                " (Linux required)"
            )

        # ── Phase 1: Disk ─────────────────────────────────────────────
        disk_out = (
            results.get("check:disk", {}).get('stdout', '').strip()
        )
        try:
            free_gb = int(disk_out) / (1024 ** 3)
            if free_gb >= MIN_DISK_GB:
                self._sys(f"✓ Disk: {free_gb:.1f} GB free")
            else:
                errors.append(
                    f"Insufficient disk: {free_gb:.1f} GB free"
                    f" ({MIN_DISK_GB} GB required)"
                )
        except (ValueError, TypeError):
            errors.append(
                f"Could not parse disk output: '{disk_out}'"
            )

        if errors:
            return errors  # OS/disk failed — skip remaining checks

        # ── Phase 2: tool verifications ───────────────────────────────
        self._sys("Phase 2/4 — Verifying installed tools...")
        for label in _VERIFY_LABELS:
            out = results.get(label, {}).get('stdout', '').strip()
            tool = label.replace("Verify ", "")
            if not out or 'FAILED' in out or 'command not found' in out:
                errors.append(f"{tool} installation failed")
                self._sys(f"✗ {tool}: installation failed")
            else:
                self._sys(f"✓ {tool}: {out.splitlines()[0]}")

        if errors:
            return errors

        # ── Phase 3: Traefik ──────────────────────────────────────────
        self._sys("Phase 3/4 — Verifying Traefik deployment...")
        for label in (
            "Create traefik directory",
            "Move traefik config files",
            "Start Traefik",
        ):
            exit_status = results.get(label, {}).get('exit_status', 1)
            if exit_status != 0:
                errors.append(
                    f"Traefik step '{label}' failed"
                    f" (exit {exit_status})"
                )
                self._sys(f"✗ Traefik: {label} failed")

        # ── Phase 4: Whitelist ────────────────────────────────────────
        self._sys("Phase 4/4 — Verifying whitelist services...")
        for label in (
            "Create globalwhitelist directory",
            "Install whitelist compose file",
            "Start whitelist services",
        ):
            exit_status = results.get(label, {}).get('exit_status', 1)
            if exit_status != 0:
                errors.append(
                    f"Whitelist step '{label}' failed"
                    f" (exit {exit_status})"
                )
                self._sys(f"✗ Whitelist: {label} failed")

        return errors

    async def on_success(self, results):
        for code in (
            'missing_deps', 'setup_failed',
            'traefik_setup_failed', 'whitelist_setup_failed',
        ):
            self._resolve_alert(code)
        self._sys("✓ Host is fully configured and ready for deployments.")
        self._host().write({'status': 'compatible', 'traefik_deployed': True})

    async def on_failure(self, results, errors):
        self._sys(f"Setup failed with {len(errors)} error(s):")
        for err in errors:
            self._sys(f"  ✗ {err}")
        self._alert(
            'setup_failed',
            "Host setup failed. Check the job log for details."
            " Re-run Setup to retry.",
            level='critical',
        )
        self._host().write({'status': 'unsupported'})
