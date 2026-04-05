"""
Setup Host Executor
-------------------
Fully prepares a remote host for doodba instance deployment:

  1. apt-get update
  2. git        — via apt (idempotent)
  3. Docker CE  — via get.docker.com script (skipped if already installed)
  4. Docker Compose V2 plugin check / install
  5. python3-pip + python3-venv (needed for pipx bootstrap)
  6. pipx
  7. copier, invoke, pre-commit  (via pipx, upgrade if already present)
  8. Verification of all tools

Every step is idempotent: re-running the job will skip already-installed
components and only install what is missing.  Sudo is required.
"""

from .abstract_executor import AbstractSSHExecutor

_BIN = 'PATH="$HOME/.local/bin:$PATH"'

# Each tuple: (label shown in logs, shell command)
SETUP_COMMANDS = [
    # ── System update ──────────────────────────────────────────────────────
    (
        "Update package index",
        "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq",
    ),
    # ── Git ───────────────────────────────────────────────────────────────
    (
        "Install git",
        "command -v git >/dev/null 2>&1 "
        "|| sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git",
    ),
    # ── Docker CE ─────────────────────────────────────────────────────────
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
    # ── Docker Compose V2 ─────────────────────────────────────────────────
    (
        "Install Docker Compose plugin",
        "docker compose version >/dev/null 2>&1 "
        "|| sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-plugin",
    ),
    # ── Python toolchain ──────────────────────────────────────────────────
    (
        "Install python3-pip and python3-venv",
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
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
    # ── Deployment tools ──────────────────────────────────────────────────
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
    # ── Zip ──────────────────────────────────────────────────
    (
        "Install zip",
        "command -v zip >/dev/null 2>&1 || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zip",
    ),
    # ── Verification ──────────────────────────────────────────────────────
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


class SetupHostExecutor(AbstractSSHExecutor):
    _job_type = "setup_host"

    def get_commands(self):
        return SETUP_COMMANDS

    async def before_execute(self, transport):
        self._sys("Starting host setup — installing all deployment dependencies...")

    def parse_results(self, results):
        errors = []
        for label in _VERIFY_LABELS:
            out = results.get(label, {}).get('stdout', '').strip()
            tool = label.replace("Verify ", "")
            if not out or 'FAILED' in out or 'command not found' in out:
                errors.append(f"{tool} verification failed")
                self._sys(f"✗ {tool}: installation failed")
            else:
                self._sys(f"✓ {tool}: {out.splitlines()[0]}")
        return errors

    async def on_success(self, results):
        self._resolve_alert('missing_deps')
        self._resolve_alert('setup_failed')
        self._sys("✓ Host fully configured and ready to deploy instances.")
        if self.job.host_id.status != 'unsupported':
            self.job.host_id.write({'status': 'compatible'})

    async def on_failure(self, results, errors):
        self._sys(f"Setup encountered {len(errors)} error(s):")
        for err in errors:
            self._sys(f"  ✗ {err}")
        self._alert(
            'setup_failed',
            "Host setup failed — one or more tools could not be installed. "
            "Check the job log for details. Re-running the job will retry "
            "only the missing components.",
            level='error',
        )
        self._sys(
            "Tip: ensure the SSH user has sudo privileges and that "
            "the host has internet access (apt + get.docker.com + PyPI)."
        )
