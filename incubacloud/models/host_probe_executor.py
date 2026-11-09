import re

from .abstract_executor import CANCELLED_WHILE_RUNNING
from .ansible_executor import AnsibleExecutor
from .cloud_host_edge_guard import edge_guard_digest

#: Free disk, in GB, under which the Check says the host is short of it.
#: The same figure as ``cloud.host._MIN_DISK_FREE_GB``, under which no new
#: instance is placed on the host, and Full Setup's own minimum.
MIN_DISK_GB = 10
GIT_MIN = (2, 24)

#: Alerts the Check raises for what it found, and resolves when it no
#: longer finds it. Each one is the reason of the host's status.
MISSING_TOOLS_ALERT = "missing_deps"
LOW_DISK_ALERT = "host_low_disk"
UNSUPPORTED_ALERT = "host_unsupported"
#: The ones that leave the host ``degraded``: the disk readings of the
#: metrics do not put it back to ``compatible`` while one is active.
CHECK_DEGRADED_ALERTS = (MISSING_TOOLS_ALERT, LOW_DISK_ALERT)


class HostProbeExecutor(AnsibleExecutor):
    """Assess a host for deployment readiness.

    ``ansible/playbooks/host_probe.yml`` gathers the OS, free disk and
    the versions of the deployment tools, and exports them. The
    *judgement* stays here in Python, and it is a verdict, not a failure:
    a Check that measured the host ends well and says what it found in
    the host's status and in an alert of its own.

    * Not Linux: ``unsupported``, nothing of the panel can run there.
    * Less than ``MIN_DISK_GB`` free, or deployment tools missing:
      ``degraded``. The instances already there keep running; with little
      disk no new one is placed on the host and a build may run short.
    * Otherwise ``compatible``.

    Only a Check that could not measure (the host unreachable, the
    playbook stopped, a disk reading it cannot parse) fails, and marks
    the host ``unsupported`` as before (until 9-nov-2026 too little disk
    failed the job as well, so its edge guard was never queued).

    The playbook also reads the digest of the host's edge guard
    (``ic_guard.yml``); when it is missing or differs from the panel's
    render, ``push_edge_guard`` is queued behind the Check.
    """

    _job_type = "host_probe"
    # The disk shown is read from metrics as soon as the job ends.
    _refreshes_host_disk = True
    _playbook = "playbooks/host_probe.yml"
    # What ``parse_results`` found: the OS when it is not Linux, the free
    # disk in GB when under ``MIN_DISK_GB``.
    _unsupported_os = None
    _low_disk_gb = None

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
        self._unsupported_os = None
        self._low_disk_gb = None

    def parse_results(self, results):
        """Read the gathered facts into the host's verdict.

        Only what keeps the Check from measuring is returned as an error
        (→ ``on_failure``): no facts at all, or a disk reading that is not
        a number. What it measured is kept for ``on_success``: an OS that
        is not Linux in ``_unsupported_os``, a disk under ``MIN_DISK_GB``
        in ``_low_disk_gb``, missing tools in ``_missing_tools``.

        :param dict results: what ran (unused: the facts are the playbook's).
        :return: list of error strings, empty when the host was measured.
        """
        errors = []
        self._missing_tools = []
        self._unsupported_os = None
        self._low_disk_gb = None
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
            self._unsupported_os = os_out or "unknown"
            self._sys(
                f"✗ OS: '{self._unsupported_os}' (Linux required)"
            )

        # ── Disk: at least MIN_DISK_GB free (less → degraded) ────────
        disk_out = str(facts.get("ic_disk_free_bytes") or "").strip()
        try:
            free_gb = int(disk_out) / (1024 ** 3)
            if free_gb >= MIN_DISK_GB:
                self._sys(f"✓ Disk: {free_gb:.1f} GB free")
            else:
                self._low_disk_gb = free_gb
                self._sys(
                    f"✗ Disk: {free_gb:.1f} GB free "
                    f"({MIN_DISK_GB} GB recommended)"
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
        """Give the host the verdict of what the Check measured.

        The status follows the worst finding (``unsupported`` for the OS,
        ``degraded`` for the disk or the tools, else ``compatible``); each
        finding raises its alert and each one gone resolves its own. The
        edge guard is read whatever the verdict.

        :param dict results: what ran (unused).
        """
        host = self.job.host_id
        if self._unsupported_os:
            self._alert(
                UNSUPPORTED_ALERT,
                f"Unsupported OS: '{self._unsupported_os}'. The panel "
                "deploys only on Linux.",
                level="critical",
            )
        else:
            self._resolve_alert(UNSUPPORTED_ALERT)
        if self._low_disk_gb is not None:
            self._alert(
                LOW_DISK_ALERT,
                f"Low disk: {self._low_disk_gb:.1f} GB free, under the "
                f"{MIN_DISK_GB} GB a new instance or a build needs. The "
                "instances on this host keep running, but no new one is "
                "placed here and a rebuild may run out of space. Free "
                "space (Docker's build cache, unused images) or grow the "
                "disk, then run Check again.",
                level="warning",
            )
        else:
            self._resolve_alert(LOW_DISK_ALERT)
        if self._missing_tools:
            missing_str = ", ".join(self._missing_tools)
            self._sys(f"⚠ Missing tools: {missing_str}")
            self._alert(
                MISSING_TOOLS_ALERT,
                f"Missing deployment tools: {missing_str}. "
                "Run 'Setup Host' to install all missing tools.",
                level="warning",
            )
        else:
            self._resolve_alert(MISSING_TOOLS_ALERT)

        if self._unsupported_os:
            host.write({"status": "unsupported"})
            self._sys("Host marked as unsupported: it does not run Linux.")
        elif self._low_disk_gb is not None or self._missing_tools:
            host.write({"status": "degraded"})
            if self._low_disk_gb is not None:
                self._sys(
                    "Host marked as degraded: low on disk. Its instances "
                    "keep running; free space before deploying more."
                )
            if self._missing_tools:
                self._sys(
                    "Host marked as degraded — run Setup Host to install "
                    "missing tools."
                )
        else:
            host.write({"status": "compatible"})
            self._sys("✓ Host is compatible and ready for deployments.")
        self._check_edge_guard()

    def _check_edge_guard(self):
        """Queue the edge guard when the host's file is missing or differs.

        Only on a host that should carry it (``_edge_guard_wanted``). A
        playbook that exported nothing about it says nothing: the job is
        not queued on a guess.
        """
        host = self.job.host_id
        if not host._edge_guard_wanted():
            return
        found = self.playbook_facts().get("ic_edge_guard_sha256")
        if found is None:
            return
        wanted = edge_guard_digest(host._shipped_edge_guard_yml())
        if (found or "").strip() == wanted:
            self._sys("✓ Edge guard: in place.")
            return
        self._sys(
            "⚠ Edge guard: missing on the host." if not (found or "").strip()
            else "⚠ Edge guard: differs from the panel's."
        )
        if host._enqueue_edge_guard("host check", chained=True):
            self._sys("Queued the edge guard for this host.")

    async def on_cancelled_running(self, results, exc):
        """A probe stopped halfway found nothing: the host keeps its status.

        ``on_failure`` marks the host unsupported for what the probe
        found, and a probe a person cancelled found nothing either way.
        """
        self._sys(f"✗ {CANCELLED_WHILE_RUNNING} The host's status is unchanged.")

    async def on_failure(self, results, errors):
        """A Check that could not measure the host marks it unsupported.

        Reached only when ``parse_results`` could not read the host (see
        there); what it measured, however bad, is ``on_success``'s.

        :param dict results: what ran.
        :param list errors: why the host could not be measured.
        """
        self._sys(f"Host probe could not check the host ({len(errors)} issue(s)):")
        for err in errors:
            self._sys(f"  ✗ {err}")
        self.job.host_id.write({"status": "unsupported"})
        self._sys("Host marked as unsupported.")
