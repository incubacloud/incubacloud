import re

from .ansible_executor import AnsibleExecutor

MIN_DISK_GB = 10
GIT_MIN = (2, 24)


class HostProbeExecutor(AnsibleExecutor):
    """Assess a host for deployment readiness.

    ``ansible/playbooks/host_probe.yml`` gathers the OS, free disk and
    the versions of the deployment tools, and exports them. The
    *judgement* stays here in Python: OS / disk are critical (a failure
    marks the host ``unsupported``), missing tools are non-fatal (the
    host is ``degraded`` and an alert points the operator at Setup Host).
    """

    _job_type = "host_probe"
    _playbook = "playbooks/host_probe.yml"

    # (probe-fact key, display name, extra-check method name or None).
    _TOOLS = [
        ("git", "git", "_check_git_version"),
        ("python3", "python3", None),
        ("docker", "docker", None),
        ("compose_v2", "docker-compose-v2", None),
        ("venv", "python3-venv", None),
        ("copier", "copier", None),
        ("invoke", "invoke", None),
        ("precommit", "pre-commit", None),
    ]

    async def before_execute(self, transport):
        self._sys("Starting host probe...")
        self._missing_tools = []

    def parse_results(self, results):
        """Turn the gathered facts into critical errors + degraded flags.

        Only OS/disk problems are returned as errors (→ ``on_failure`` →
        unsupported). Missing tools are collected in ``_missing_tools``
        and handled in ``on_success`` as a degraded state, exactly as the
        pre-Ansible executor did.
        """
        errors = []
        self._missing_tools = []
        facts = self.playbook_facts()

        if not facts:
            # The playbook did not export its results — it never got far
            # enough to gather anything (the rc is in ``results``).
            return ["Host probe did not complete — see the job log."]

        # ── OS: must be Linux ────────────────────────────────────────
        os_out = (facts.get("ic_os") or "").strip()
        if os_out.lower() == "linux":
            self._sys(f"✓ OS: {os_out}")
        else:
            errors.append(
                f"Unsupported OS: '{os_out or 'unknown'}' (Linux required)"
            )

        # ── Disk: at least MIN_DISK_GB free ──────────────────────────
        disk_out = str(facts.get("ic_disk_free_bytes") or "").strip()
        try:
            free_gb = int(disk_out) / (1024 ** 3)
            if free_gb >= MIN_DISK_GB:
                self._sys(f"✓ Disk: {free_gb:.1f} GB free")
            else:
                errors.append(
                    f"Insufficient disk: {free_gb:.1f} GB free "
                    f"({MIN_DISK_GB} GB required)"
                )
        except (ValueError, TypeError):
            errors.append(f"Could not parse disk output: '{disk_out}'")

        # ── Required deployment tools (missing → degraded) ───────────
        tools = facts.get("ic_tools") or {}
        for key, display_name, extra_check in self._TOOLS:
            self._check_tool(tools, key, display_name, extra_check)

        return errors

    def _check_tool(self, tools, key, display_name, extra_check=None):
        """Flag a tool as missing / too old, or log it as present."""
        out = (tools.get(key) or "").strip()
        if not out or "MISSING" in out or "command not found" in out:
            self._missing_tools.append(display_name)
            self._sys(f"✗ {display_name}: not found")
            return
        if extra_check:
            err = getattr(self, extra_check)(out)
            if err:
                self._missing_tools.append(display_name)
                self._sys(f"✗ {display_name}: {err}")
                return
        self._sys(f"✓ {display_name}: {out.splitlines()[0]}")

    def _check_git_version(self, out):
        """Return an error string if git is below GIT_MIN, else None."""
        m = re.search(r"(\d+)\.(\d+)", out)
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
            missing_str = ", ".join(self._missing_tools)
            self._sys(f"⚠ Missing tools: {missing_str}")
            self._alert(
                "missing_deps",
                f"Missing deployment tools: {missing_str}. "
                "Run 'Setup Host' to install all missing tools.",
                level="warning",
            )
            self.job.host_id.write({"status": "degraded"})
            self._sys(
                "Host marked as degraded — run Setup Host to install "
                "missing tools."
            )
        else:
            self._resolve_alert("missing_deps")
            self.job.host_id.write({"status": "compatible"})
            self._sys("✓ Host is compatible and ready for deployments.")

    async def on_failure(self, results, errors):
        self._sys(f"Host probe found {len(errors)} critical issue(s):")
        for err in errors:
            self._sys(f"  ✗ {err}")
        self.job.host_id.write({"status": "unsupported"})
        self._sys("Host marked as unsupported.")
