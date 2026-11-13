"""
Tier 2 — the host-state executors that Phase 3 moved onto Ansible.

``host_probe``, ``delete_host``, ``docker_prune`` and ``setup_whitelist``
now run playbooks instead of composing bash. The playbooks themselves
are covered by ansible-lint and an end-to-end smoke run; what these
tests pin is the Python that survived the move — the *judgement* that
reads the facts a playbook exports and the model writes it drives.

Facts are injected straight into ``_facts`` (what ``playbook_facts()``
returns), the same way the old executor tests fed a ``results`` dict:
no host, no ansible-runner.
"""
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_host_edge_guard import edge_guard_digest
from odoo.addons.incubacloud.models.cloud_host_shared_guard import (
    SHARED_GUARD_ALERT,
)
from odoo.addons.incubacloud.models.host_probe_executor import (
    GIT_MIN,
    LOW_DISK_ALERT,
    MISSING_TOOLS_ALERT,
    UNSUPPORTED_ALERT,
    HostProbeExecutor,
)
from odoo.addons.incubacloud.models.docker_prune_executor import (
    DockerPruneExecutor,
)
from odoo.addons.incubacloud.models.delete_host_executor import (
    DeleteHostExecutor,
)
from odoo.addons.incubacloud.models.setup_whitelist_executor import (
    SetupWhitelistExecutor,
    build_whitelist_compose,
)


def _run(coro):
    """Drive a coroutine to completion without an event loop fixture."""
    import asyncio

    return asyncio.run(coro)


class HostStateCase(TransactionCase):

    def _executor(self, cls, *, host=None, facts=None):
        """Build an executor without ``__init__`` (no host, no runner)."""
        ex = object.__new__(cls)
        ex.job = MagicMock(spec=type(self.env["cloud.job"]))
        ex.job.id = 7
        ex.job.host_id = host if host is not None else self._host()
        ex.env = self.env
        ex._log_buffer = []
        ex._facts = facts or {}
        return ex

    def _host(self, **kw):
        return self.env["cloud.host"].create(
            {
                "name": "H",
                "ip_address": "10.0.0.1",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
            }
            | kw
        )


ENOUGH_DISK = str(50 * 1024 ** 3)  # 50 GiB in bytes

ALL_TOOLS_OK = {
    "git": "git version 2.39.5",
    "python3": "Python 3.12.13",
    "docker": "Docker version 27.0.0",
    "compose_v2": "Docker Compose version v2.29.0",
    "venv": "OK",
    "copier": "copier 9.2.0",
    "invoke": "invoke 2.2.0",
    "precommit": "pre-commit 3.7.0",
}


def _probe_facts(*, os="Linux", disk=ENOUGH_DISK, tools=None):
    return {
        "ic_os": os,
        "ic_disk_free_bytes": disk,
        "ic_tools": ALL_TOOLS_OK if tools is None else tools,
    }


class TestHostProbeJudgement(HostStateCase):

    def _probe(self, **facts_kw):
        ex = self._executor(HostProbeExecutor, facts=_probe_facts(**facts_kw))
        ex._missing_tools = []
        return ex

    def test_a_healthy_host_has_no_errors(self):
        ex = self._probe()
        self.assertEqual(ex.parse_results({}), [])
        self.assertEqual(ex._missing_tools, [])

    def test_a_healthy_host_is_marked_compatible(self):
        ex = self._probe()
        ex.parse_results({})
        _run(ex.on_success({}))
        self.assertEqual(ex.job.host_id.status, "compatible")

    def _alerting(self, ex):
        """Let *ex* file its alerts in this test's transaction.

        Its durable cursor rides on the test's (registry test mode), and
        the alerts name no job: the mocked one has no row.
        """
        if not getattr(self, "_in_test_mode", False):
            self.registry_enter_test_mode()
            self._in_test_mode = True
        ex.job.env = self.env
        ex.job.id = False
        return ex

    def _active(self, code, host):
        """Return the active alerts *code* of *host*."""
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", code),
            ("host_id", "=", host.id),
            ("state", "=", "active"),
        ])

    def test_non_linux_is_a_verdict_not_an_error(self):
        """The Check measured the host: it ends well and says so."""
        ex = self._alerting(self._probe(os="Darwin"))
        self.assertEqual(ex.parse_results({}), [])
        _run(ex.on_success({}))
        host = ex.job.host_id
        self.assertEqual(host.status, "unsupported")
        alert = self._active(UNSUPPORTED_ALERT, host)
        self.assertEqual(alert.level, "critical")
        self.assertIn("Darwin", alert.message)

    def test_a_check_that_could_not_measure_marks_the_host_unsupported(self):
        ex = self._executor(HostProbeExecutor, facts={})
        errors = ex.parse_results({})
        _run(ex.on_failure({}, errors))
        self.assertEqual(ex.job.host_id.status, "unsupported")

    def test_too_little_disk_is_a_verdict_not_an_error(self):
        """9-nov-2026: 7.3 GB free failed the Check and marked the host
        unsupported, though its three instances ran on."""
        ex = self._alerting(self._probe(disk=str(int(7.3 * 1024 ** 3))))
        self.assertEqual(ex.parse_results({}), [])
        _run(ex.on_success({}))
        host = ex.job.host_id
        self.assertEqual(host.status, "degraded")
        alert = self._active(LOW_DISK_ALERT, host)
        self.assertEqual(alert.level, "warning")
        self.assertIn("7.3 GB free", alert.message)
        self.assertFalse(self._active(MISSING_TOOLS_ALERT, host))

    def test_enough_disk_resolves_the_low_disk_alert(self):
        ex = self._alerting(self._probe(disk=str(3 * 1024 ** 3)))
        ex.parse_results({})
        _run(ex.on_success({}))
        host = ex.job.host_id
        ex = self._alerting(
            self._executor(HostProbeExecutor, host=host, facts=_probe_facts()),
        )
        ex.parse_results({})
        _run(ex.on_success({}))
        self.assertEqual(host.status, "compatible")
        self.assertFalse(self._active(LOW_DISK_ALERT, host))

    def test_linux_again_resolves_the_unsupported_alert(self):
        ex = self._alerting(self._probe(os="Darwin"))
        ex.parse_results({})
        _run(ex.on_success({}))
        host = ex.job.host_id
        ex = self._alerting(
            self._executor(HostProbeExecutor, host=host, facts=_probe_facts()),
        )
        ex.parse_results({})
        _run(ex.on_success({}))
        self.assertEqual(host.status, "compatible")
        self.assertFalse(self._active(UNSUPPORTED_ALERT, host))

    def test_unparseable_disk_is_a_critical_error(self):
        errors = self._probe(disk="not-a-number").parse_results({})
        self.assertTrue(any("Could not parse disk" in e for e in errors))

    def test_a_missing_tool_is_degraded_not_critical(self):
        tools = ALL_TOOLS_OK | {"copier": "MISSING"}
        ex = self._probe(tools=tools)
        errors = ex.parse_results({})
        # No critical error…
        self.assertEqual(errors, [])
        # …but the tool is flagged.
        self.assertIn("copier", ex._missing_tools)

    def test_a_missing_tool_marks_the_host_degraded(self):
        ex = self._probe(tools=ALL_TOOLS_OK | {"invoke": "MISSING"})
        ex.parse_results({})
        _run(ex.on_success({}))
        self.assertEqual(ex.job.host_id.status, "degraded")

    def test_tools_found_close_the_ones_dismissed_by_hand(self):
        """A missing-tools alert dismissed by hand keeps the host out of
        the readings' reach until a Check finds the tools: that Check
        closes it as resolved, and the readings may restore the host."""
        ex = self._alerting(self._probe(tools=ALL_TOOLS_OK | {"invoke": "MISSING"}))
        ex.parse_results({})
        _run(ex.on_success({}))
        host = ex.job.host_id
        alert = self._active(MISSING_TOOLS_ALERT, host)
        alert.write({"state": "dismissed", "resolution": "manual"})
        self.assertTrue(host._check_keeps_degraded())
        ex = self._alerting(
            self._executor(HostProbeExecutor, host=host, facts=_probe_facts()),
        )
        ex.parse_results({})
        _run(ex.on_success({}))
        self.env.invalidate_all()
        self.assertEqual(alert.resolution, "auto")
        self.assertEqual(host.status, "compatible")
        host.write({"status": "degraded", "disk_free_gb": 40.0})
        self.assertFalse(host._check_keeps_degraded())

    def test_an_old_git_is_flagged(self):
        old = f"git version {GIT_MIN[0]}.{GIT_MIN[1] - 1}.0"
        ex = self._probe(tools=ALL_TOOLS_OK | {"git": old})
        ex.parse_results({})
        self.assertIn("git", ex._missing_tools)

    def test_a_new_enough_git_passes(self):
        new = f"git version {GIT_MIN[0]}.{GIT_MIN[1] + 5}.0"
        ex = self._probe(tools=ALL_TOOLS_OK | {"git": new})
        ex.parse_results({})
        self.assertNotIn("git", ex._missing_tools)

    def test_empty_stdout_counts_as_missing(self):
        # A tool that printed nothing is treated as absent, not present.
        ex = self._probe(tools=ALL_TOOLS_OK | {"docker": ""})
        ex.parse_results({})
        self.assertIn("docker", ex._missing_tools)

    def test_no_facts_means_the_probe_did_not_complete(self):
        ex = self._executor(HostProbeExecutor, facts={})
        ex._missing_tools = []
        errors = ex.parse_results({})
        self.assertTrue(errors)
        self.assertIn("did not complete", errors[0])


class TestTheCheckReadsTheEdgeGuard(HostStateCase):
    """The playbook exports the digest of ``ic_guard.yml``; the Check
    queues ``push_edge_guard`` only when the file is missing or differs,
    and only on a host that should carry it. It stays read-only."""

    def _check(self, digest, disk=ENOUGH_DISK, **host_vals):
        host = self._host(**({"traefik_deployed": True} | host_vals))
        facts = _probe_facts(disk=disk)
        if digest is not None:
            facts["ic_edge_guard_sha256"] = digest
        ex = self._executor(HostProbeExecutor, host=host, facts=facts)
        ex._missing_tools = []
        ex.parse_results({})
        with patch.object(
            type(host), "_enqueue_edge_guard", autospec=True, return_value=1,
        ) as enqueue:
            _run(ex.on_success({}))
        return host, enqueue

    def _wanted(self):
        return edge_guard_digest(
            self.env["cloud.host"]._shipped_edge_guard_yml(),
        )

    def test_a_missing_guard_is_queued_behind_the_check(self):
        host, enqueue = self._check("")
        enqueue.assert_called_once_with(host, "host check", chained=True)

    def test_a_host_short_of_disk_still_gets_its_guard(self):
        """The verdict of little disk is no reason to leave it unguarded."""
        host, enqueue = self._check("", disk=str(7 * 1024 ** 3))
        enqueue.assert_called_once_with(host, "host check", chained=True)
        self.assertEqual(host.status, "degraded")

    def test_a_different_guard_is_queued(self):
        host, enqueue = self._check("0" * 64)
        enqueue.assert_called_once_with(host, "host check", chained=True)

    def test_the_same_guard_queues_nothing(self):
        _host, enqueue = self._check(self._wanted())
        enqueue.assert_not_called()

    def test_a_host_without_the_panels_proxy_queues_nothing(self):
        _host, enqueue = self._check("", traefik_deployed=False)
        enqueue.assert_not_called()

    def test_a_playbook_that_said_nothing_queues_nothing(self):
        _host, enqueue = self._check(None)
        enqueue.assert_not_called()

    def test_the_playbook_only_reads(self):
        playbook = (
            Path(__file__).resolve().parents[1]
            / "ansible" / "playbooks" / "host_probe.yml"
        )
        plays = yaml.safe_load(playbook.read_text())
        task = next(
            t for t in plays[0]["tasks"]
            if t["name"] == "Read the edge guard's digest"
        )
        self.assertEqual(task["ansible.builtin.command"]["argv"][0], "sha256sum")
        self.assertIs(task["changed_when"], False)
        self.assertIs(task["failed_when"], False)
        exported = plays[0]["tasks"][-1]["ansible.builtin.set_stats"]["data"]
        self.assertIn("ic_edge_guard_sha256", exported)


class TestTheCheckReadsTheProxyNetworkGuard(HostStateCase):
    """The playbook exports the last state the guard on the proxy network
    wrote; the Check raises ``shared_guard_open`` when it says open or
    unsupported and resolves it when it says on, so a guard that opened
    after Full Setup reaches the panel."""

    def _check(self, line, **host_vals):
        host = self._host(**host_vals)
        facts = _probe_facts()
        if line is not None:
            facts["ic_shared_guard_state"] = line
        ex = self._executor(HostProbeExecutor, host=host, facts=facts)
        ex._missing_tools = []
        ex.parse_results({})
        _run(ex.on_success({}))
        return host

    def _active(self, host):
        return self.env["cloud.alert"].search([
            ("host_id", "=", host.id),
            ("code", "=", SHARED_GUARD_ALERT),
            ("state", "=", "active"),
        ])

    def test_a_guard_that_opened_raises_its_alert(self):
        host = self._check(
            "IC_SHARED_GUARD_UNSUPPORTED the firewall has no MAC match (xt_mac)",
        )
        alert = self._active(host)
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")
        self.assertIn("the firewall has no MAC match (xt_mac)", alert.message)

    def test_a_guard_on_again_resolves_it(self):
        host = self._check("IC_SHARED_GUARD_OPEN the proxy is not running on x")
        self.assertTrue(self._active(host))
        ex = self._executor(
            HostProbeExecutor, host=host,
            facts=_probe_facts() | {
                "ic_shared_guard_state": "IC_SHARED_GUARD_ON bridge br-x, proxy m",
            },
        )
        ex._missing_tools = []
        ex.parse_results({})
        _run(ex.on_success({}))
        self.assertFalse(self._active(host))

    def test_no_line_or_the_switch_off_says_nothing(self):
        for line, vals in (
            (None, {}),
            ("", {}),
            ("IC_SHARED_GUARD_OPEN x", {"shared_guard": False}),
        ):
            host = self._check(line, **vals)
            self.assertFalse(self._active(host))

    def test_the_playbook_reads_the_file_the_guard_writes(self):
        root = Path(__file__).resolve().parents[1]
        plays = yaml.safe_load(
            (root / "ansible" / "playbooks" / "host_probe.yml").read_text(),
        )
        task = next(
            t for t in plays[0]["tasks"]
            if t["name"] == "Read the last state of the guard on the proxy network"
        )
        path = task["ansible.builtin.command"]["argv"][1]
        self.assertIn(
            f'STATE_FILE="${{IC_SHARED_GUARD_STATE:-{path}}}"',
            (root / "scripts" / "shared_guard.sh").read_text(),
        )
        self.assertIs(task["changed_when"], False)
        self.assertIs(task["failed_when"], False)
        exported = plays[0]["tasks"][-1]["ansible.builtin.set_stats"]["data"]
        self.assertIn("ic_shared_guard_state", exported)


class TestDockerPruneSummary(HostStateCase):

    def test_the_reclaimed_line_is_surfaced(self):
        ex = self._executor(
            DockerPruneExecutor,
            facts={"ic_prune_stdout": "Deleted images...\nTotal reclaimed space: 1.5GB"},
        )
        _run(ex.on_success({}))
        self.assertTrue(
            any("1.5GB" in line for line, _src in ex._log_buffer),
        )

    def test_a_prune_with_no_summary_still_reports_completion(self):
        ex = self._executor(DockerPruneExecutor, facts={"ic_prune_stdout": ""})
        _run(ex.on_success({}))
        self.assertTrue(
            any("complete" in line for line, _src in ex._log_buffer),
        )


class TestDeleteHostArchive(HostStateCase):

    def test_success_archives_the_host(self):
        host = self._host()
        ex = self._executor(DeleteHostExecutor, host=host)
        _run(ex.on_success({}))
        self.assertFalse(host.active)


class TestSetupWhitelistExtraVars(HostStateCase):

    def _executor_with_hostnames(self, hostnames):
        host = self._host()
        for hostname in hostnames:
            self.env["cloud.host.whitelist"].create({
                "host_id": host.id,
                "hostname": hostname,
            })
        return self._executor(SetupWhitelistExecutor, host=host)

    def test_extra_vars_carry_the_generated_compose(self):
        ex = self._executor_with_hostnames(["fonts.googleapis.com"])
        extra = ex.get_extra_vars()
        self.assertIn("ic_whitelist_compose", extra)
        # The value is exactly what the shared builder produces for the
        # host's entries (a fresh host also carries default entries).
        self.assertEqual(
            extra["ic_whitelist_compose"],
            build_whitelist_compose(ex._hostnames()),
        )

    def test_the_compose_names_every_whitelisted_host(self):
        ex = self._executor_with_hostnames(["a.example.com", "b.example.com"])
        compose = ex.get_extra_vars()["ic_whitelist_compose"]
        self.assertIn("a.example.com", compose)
        self.assertIn("b.example.com", compose)

    def test_every_proxy_runs_the_pinned_build(self):
        """By digest, never ``latest``: a host set up later must proxy
        through the build the others run, not whatever was published."""
        compose = build_whitelist_compose(["a.example.com", "b.example.com"])
        images = re.findall(r"^    image: (\S+)$", compose, re.MULTILINE)
        self.assertEqual(len(images), 2)
        for image in images:
            self.assertRegex(
                image,
                r"^ghcr\.io/tecnativa/docker-whitelist:[0-9.]+@sha256:[0-9a-f]{64}$",
            )
