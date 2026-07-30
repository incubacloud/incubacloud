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
from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.host_probe_executor import (
    GIT_MIN,
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

    def test_non_linux_is_a_critical_error(self):
        errors = self._probe(os="Darwin").parse_results({})
        self.assertTrue(any("Unsupported OS" in e for e in errors))

    def test_a_critical_error_marks_the_host_unsupported(self):
        ex = self._probe(os="Darwin")
        errors = ex.parse_results({})
        _run(ex.on_failure({}, errors))
        self.assertEqual(ex.job.host_id.status, "unsupported")

    def test_too_little_disk_is_a_critical_error(self):
        errors = self._probe(disk=str(3 * 1024 ** 3)).parse_results({})
        self.assertTrue(any("Insufficient disk" in e for e in errors))

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
