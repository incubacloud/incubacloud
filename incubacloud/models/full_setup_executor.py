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
"""

from .abstract_executor import AbstractSSHExecutor
from .setup_host_executor import SETUP_COMMANDS, _VERIFY_LABELS
from .setup_traefik_executor import _build_inverseproxy, _TMP
from .setup_whitelist_executor import (
    build_whitelist_compose,
    _WL_TMP, _WL_DIR, _WL_FILE, _WL_PROJECT,
)

MIN_DISK_GB = 10


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
            level='error',
        )
        self._host().write({'status': 'unsupported'})
