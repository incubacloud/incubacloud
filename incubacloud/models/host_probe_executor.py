import re

from .abstract_executor import AbstractSSHExecutor

MIN_DISK_GB = 10
GIT_MIN = (2, 24)

# pipx-installed tools land in ~/.local/bin which is absent from default SSH PATH
_BIN = 'PATH="$HOME/.local/bin:$PATH"'

# (label, shell command) — label is the key in parse_results dict
TOOL_CHECKS = [
    ("tool:git",        'git --version 2>&1 || echo MISSING'),
    ("tool:python3",    'python3 --version 2>&1 || echo MISSING'),
    ("tool:docker",     'docker --version 2>&1 || echo MISSING'),
    ("tool:compose_v2", 'docker compose version 2>&1 || echo MISSING'),
    ("tool:venv",       'python3 -c "import venv; print(\'OK\')" 2>&1 || echo MISSING'),
    ("tool:copier",     f'{_BIN} copier --version 2>&1 || echo MISSING'),
    ("tool:invoke",     f'{_BIN} invoke --version 2>&1 || echo MISSING'),
    ("tool:precommit",  f'{_BIN} pre-commit --version 2>&1 || echo MISSING'),
]


class HostProbeExecutor(AbstractSSHExecutor):
    _job_type = "host_probe"

    def get_commands(self):
        return [
            ("Detecting operating system",    "uname -s"),
            ("Checking available disk space", "df -B1 --output=avail / | tail -n 1"),
        ] + TOOL_CHECKS

    async def before_execute(self, transport):
        self._sys("Starting host probe...")
        self._missing_tools = []

    def parse_results(self, results):
        errors = []
        self._missing_tools = []

        # ── OS: must be Linux ────────────────────────────────────────────────
        os_out = results.get("Detecting operating system", {}).get('stdout', '').strip()
        if os_out.lower() == 'linux':
            self._sys(f"✓ OS: {os_out}")
        else:
            errors.append(f"Unsupported OS: '{os_out or 'unknown'}' (Linux required)")

        # ── Disk: at least MIN_DISK_GB free ──────────────────────────────────
        disk_out = results.get("Checking available disk space", {}).get('stdout', '').strip()
        try:
            free_bytes = int(disk_out)
            free_gb = free_bytes / (1024 ** 3)
            if free_gb >= MIN_DISK_GB:
                self._sys(f"✓ Disk: {free_gb:.1f} GB free")
            else:
                errors.append(
                    f"Insufficient disk: {free_gb:.1f} GB free ({MIN_DISK_GB} GB required)"
                )
        except (ValueError, TypeError):
            errors.append(f"Could not parse disk output: '{disk_out}'")

        # ── Required deployment tools (missing → degraded, not unsupported) ──
        self._check_tool(results, "tool:git",        "git",          self._check_git_version)
        self._check_tool(results, "tool:python3",    "python3",      None)
        self._check_tool(results, "tool:docker",     "docker",       None)
        self._check_tool(results, "tool:compose_v2", "docker-compose-v2", None)
        self._check_tool(results, "tool:venv",       "python3-venv", None)
        self._check_tool(results, "tool:copier",     "copier",       None)
        self._check_tool(results, "tool:invoke",     "invoke",       None)
        self._check_tool(results, "tool:precommit",  "pre-commit",   None)

        # Only critical infrastructure errors reach on_failure;
        # tool gaps are surfaced in on_success as a degraded state.
        return errors

    def _check_tool(self, results, key, display_name, extra_check=None):
        """Check one tool result; append to self._missing_tools if absent/wrong."""
        out = results.get(key, {}).get('stdout', '').strip()
        if not out or 'MISSING' in out or 'command not found' in out:
            self._missing_tools.append(display_name)
            self._sys(f"✗ {display_name}: not found")
            return
        if extra_check:
            err = extra_check(out)
            if err:
                self._missing_tools.append(display_name)
                self._sys(f"✗ {display_name}: {err}")
                return
        self._sys(f"✓ {display_name}: {out.splitlines()[0]}")

    def _check_git_version(self, out):
        """Return an error string if git version is below GIT_MIN, else None."""
        m = re.search(r'(\d+)\.(\d+)', out)
        if not m:
            return "could not parse version"
        major, minor = int(m.group(1)), int(m.group(2))
        if (major, minor) < GIT_MIN:
            return (
                f"version {major}.{minor} is too old "
                f"({GIT_MIN[0]}.{GIT_MIN[1]}+ required)"
            )
        return None

    async def on_success(self, results):
        if self._missing_tools:
            missing_str = ', '.join(self._missing_tools)
            self._sys(f"⚠ Missing tools: {missing_str}")
            self._alert(
                'missing_deps',
                f"Missing deployment tools: {missing_str}. "
                "Run 'Setup Host' to install all missing tools.",
                level='warning',
            )
            self.job.host_id.write({'status': 'degraded'})
            self._sys("Host marked as degraded — run Setup Host to install missing tools.")
        else:
            self._resolve_alert('missing_deps')
            self.job.host_id.write({'status': 'compatible'})
            self._sys("✓ Host is compatible and ready for deployments.")

    async def on_failure(self, results, errors):
        self._sys(f"Host probe found {len(errors)} critical issue(s):")
        for err in errors:
            self._sys(f"  ✗ {err}")
        self.job.host_id.write({'status': 'unsupported'})
        self._sys("Host marked as unsupported.")
