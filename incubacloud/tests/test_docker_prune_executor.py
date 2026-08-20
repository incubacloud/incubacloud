"""Tests for the docker_prune executor's protect-label filter.

The filter is what keeps ``docker system prune -af`` from sweeping
containers that are stopped on purpose (the SaaS warm pool). It was
already designed once and silently lost: a SaaS-side monkey-patch of
``get_commands`` implemented it, and the method stopped being called the
day this executor moved to ``AnsibleExecutor`` — nothing failed, the
prune just went back to unfiltered and wiped a warm instance's
containers between its rebuild and that night's backup.

So these assertions pin the *mechanism* (Python and playbook agreeing on
one label, reaching the actual prune command) rather than any wording.

The play deliberately does not run ``docker system prune``: that prunes
networks too, and a project network can be neither labelled (compose
recreates it and tears the live stack down) nor safely swept (a stopped
container releases its endpoint, so the network counts as unused and
goes — taking nothing but the stack's ability to start). Pruning only
what occupies disk avoids the choice, so the tests below pin the absence
of ``system prune`` as hard as they pin the label.
"""
import os
from unittest.mock import patch

import yaml

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.docker_prune_executor import (
    NETWORK_ALERT_CODE,
    PROTECT_LABEL,
    SWEPT_ALERT_CODE,
    DockerPruneExecutor,
    _format_size,
    _reclaimed_bytes,
)


class TestDockerPruneProtectFilter(TransactionCase):

    def _play(self):
        """Return the parsed maintenance playbook (single play)."""
        path = os.path.join(
            get_module_path("incubacloud"),
            "ansible", "playbooks", "host_maintenance.yml",
        )
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)[0]

    def _command_tasks(self):
        return [
            task for task in (self._play().get("tasks") or [])
            if "ansible.builtin.command" in task
        ]

    def _prune_cmds(self):
        """Return the prune commands the play actually runs."""
        prune_tasks = [
            task for task in self._command_tasks() if "loop" in task
        ]
        self.assertEqual(
            len(prune_tasks), 1, "the play must run one looped prune task",
        )
        return prune_tasks[0]["loop"]

    def test_the_prune_never_touches_networks(self):
        """The regression this play caused, pinned.

        ``docker system prune`` also prunes networks. A project network
        cannot be protected by the label — labelling it makes compose
        recreate it mid-command — and a stopped stack's network counts
        as unused, so the prune deleted the networks of both ``ready``
        warm spares while their labelled containers survived. Neither
        stack could start afterwards: a container that is started
        rather than recreated keeps the network id it was born with.
        """
        for cmd in self._prune_cmds():
            self.assertNotIn(
                "system prune", cmd,
                "system prune takes networks with it; prune the "
                "resources that actually occupy disk instead",
            )
            self.assertNotIn("network prune", cmd)

    def test_prune_covers_what_actually_occupies_disk(self):
        """Dropping networks must not cost the reclaim it was doing."""
        joined = "\n".join(self._prune_cmds())
        self.assertIn("docker container prune", joined)
        self.assertIn("docker image prune", joined)
        self.assertIn("docker builder prune", joined)

    def test_prune_command_excludes_protected_resources(self):
        """The prunes that can sweep a stopped stack carry the filter.

        Build cache has nothing to protect, so it is exempt; containers
        and images both do.
        """
        for cmd in self._prune_cmds():
            if "builder prune" in cmd:
                continue
            self.assertIn(
                '--filter "label!={{ ic_protect_label }}"', cmd,
            )

    def test_playbook_default_matches_the_python_constant(self):
        """Standalone runs prune exactly what a job run prunes."""
        self.assertEqual(
            self._play()["vars"]["ic_protect_label"], PROTECT_LABEL,
        )

    def test_executor_feeds_the_label_to_the_playbook(self):
        """The value that actually runs comes from ``PROTECT_LABEL``."""
        executor = DockerPruneExecutor.__new__(DockerPruneExecutor)
        self.assertEqual(
            executor.get_extra_vars(), {"ic_protect_label": PROTECT_LABEL},
        )

    def test_playbook_inventories_survivors(self):
        """A container inventory runs after the prune and is exported.

        The presence check in ``on_success`` is only as good as this
        fact: without it the executor silently skips the verification.
        """
        cmds = [
            task["ansible.builtin.command"]["cmd"]
            for task in self._command_tasks()
        ]
        inventory = [cmd for cmd in cmds if "docker ps -a" in cmd]
        self.assertEqual(len(inventory), 1)
        self.assertIn("{{.Names}}", inventory[0])
        networks = [cmd for cmd in cmds if "docker network ls" in cmd]
        self.assertEqual(
            len(networks), 1,
            "the network check in on_success is only as good as this fact",
        )
        self.assertIn("{{.Name}}", networks[0])
        stats = [
            task for task in (self._play().get("tasks") or [])
            if "ansible.builtin.set_stats" in task
        ]
        self.assertEqual(len(stats), 1)
        exported = stats[0]["ansible.builtin.set_stats"]["data"]
        self.assertIn("ic_containers_after", exported)
        self.assertIn("ic_prune_stdout", exported)
        self.assertIn("ic_networks_after", exported)


class TestPruneSummary(TransactionCase):
    """Pure parsing of the prune stdout into log lines."""

    def _executor(self):
        executor = DockerPruneExecutor.__new__(DockerPruneExecutor)
        executor._log_buffer = []
        return executor

    def _logged(self, executor):
        return [msg for msg, _src in executor._log_buffer]

    def test_sections_are_counted_and_networks_named(self):
        executor = self._executor()
        executor._summarize_prune_output(
            "Deleted Containers:\nabc123\ndef456\n\n"
            "Deleted Networks:\nic-tenant-x_default\n\n"
            "Deleted Images:\nuntagged: foo\ndeleted: sha256:bar\n\n"
            "Total reclaimed space: 5.9GB"
        )
        logged = "\n".join(self._logged(executor))
        self.assertIn("2 containers", logged)
        self.assertIn("1 networks", logged)
        self.assertIn("2 images", logged)
        self.assertIn("ic-tenant-x_default", logged)

    def test_empty_output_logs_nothing(self):
        executor = self._executor()
        executor._summarize_prune_output("Total reclaimed space: 0B")
        self.assertEqual(self._logged(executor), [])


class TestReclaimedTotal(TransactionCase):
    """The play prints one total per prune; the log must show the sum."""

    def test_three_totals_are_added_up(self):
        """Reading the output backwards would report only the last one."""
        stdout = (
            "Total reclaimed space: 0B\n"
            "Total reclaimed space: 11.2GB\n"
            "Total reclaimed space: 1.66GB\n"
        )
        self.assertEqual(
            _format_size(_reclaimed_bytes(stdout)), "12.86GB",
            "the sum, not the build cache's line alone",
        )

    def test_units_are_si_like_docker(self):
        self.assertEqual(_reclaimed_bytes("Total reclaimed space: 938.4kB"),
                         938400)
        self.assertEqual(_reclaimed_bytes("Total reclaimed space: 5B"), 5)

    def test_no_total_is_zero_not_a_crash(self):
        self.assertEqual(_reclaimed_bytes("nothing here"), 0)
        self.assertEqual(_format_size(0), "0B")


class TestPostPruneNetworkGuard(TransactionCase):
    """The post-prune presence check on panel-managed networks.

    This is the check that was missing on 2026-08-20: the containers
    were all present, the guard said every stack was intact, and both
    warm spares were unstartable because their network was gone.
    """

    def setUp(self):
        super().setUp()
        # Same reason as the container guard below: the alert is written
        # on a cursor of its own and would not see this transaction's
        # host without test mode.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "Net Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "net-host",
            "ip_address": "192.0.2.63",
            "user": "ubuntu",
            "wildcard_domain": "net.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "netinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "state": "deployed",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "docker_prune")], limit=1,
        )
        self.job = self.env["cloud.job"].create({
            "name": "Prune",
            "host_id": self.host.id,
            "job_type_id": job_type.id,
        })
        self.executor = DockerPruneExecutor(self.job, self.host)

    def _alert(self):
        return self.env["cloud.alert"].search([
            ("code", "=", NETWORK_ALERT_CODE),
            ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])

    def test_surviving_network_raises_nothing(self):
        self.executor._check_managed_networks(
            f"bridge\nhost\n{self.instance.doodba_project_name}_default",
        )
        self.assertFalse(self._alert())

    def test_missing_network_raises_critical(self):
        """The production shape: containers fine, network gone."""
        self.executor._check_managed_networks("bridge\nhost\nnone")
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertIn(
            f"{self.instance.doodba_project_name}_default", alert.message,
        )

    def test_recovered_network_resolves_the_alert(self):
        self.executor._check_managed_networks("bridge")
        self.assertTrue(self._alert())
        self.executor._check_managed_networks(
            f"{self.instance.doodba_project_name}_default",
        )
        self.assertFalse(self._alert())

    def test_busy_instance_is_exempt(self):
        """A rebuild drops the network on its way down; that is normal."""
        Job = self.env["cloud.job"]
        with patch.object(
            type(Job), "search_count", autospec=True, return_value=1,
        ):
            self.executor._check_managed_networks("bridge")
        self.assertFalse(self._alert())


class TestPostPruneGuard(TransactionCase):
    """The post-prune presence check on panel-managed containers."""

    def setUp(self):
        super().setUp()
        # The guard raises/resolves its alert on a cursor of its own, so
        # the row survives a job that later fails. A plain
        # TransactionCase leaves that cursor unable to see the host and
        # instance created here, and the alert's foreign keys blow up.
        # Test mode makes the registry hand out cursors wrapping this
        # test's transaction, keeping the write real and rolled back.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "Guard Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "guard-host",
            "ip_address": "192.0.2.62",
            "user": "ubuntu",
            "wildcard_domain": "guard.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "guardinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            # ``deployed`` is derived — seeding the lifecycle state is
            # the supported way to import an already-running instance.
            "state": "deployed",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "docker_prune")], limit=1,
        )
        self.job = self.env["cloud.job"].create({
            "name": "Prune",
            "host_id": self.host.id,
            "job_type_id": job_type.id,
        })
        self.executor = DockerPruneExecutor(self.job, self.host)

    def _survivors(self, *names):
        return "\n".join(names)

    def _alert(self):
        return self.env["cloud.alert"].search([
            ("code", "=", SWEPT_ALERT_CODE),
            ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])

    def test_intact_stack_raises_nothing(self):
        project = self.instance.doodba_project_name
        self.executor._check_managed_containers(
            self._survivors(f"{project}-odoo-1", f"{project}-db-1"),
        )
        self.assertFalse(self._alert())

    def test_missing_managed_container_raises_critical(self):
        project = self.instance.doodba_project_name
        # db survived (running while asleep), odoo was swept — the
        # exact shape of a pruned Sablier-slept tenant.
        self.executor._check_managed_containers(
            self._survivors(f"{project}-db-1"),
        )
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertIn(f"{project}-odoo-1", alert.message)

    def test_recovered_stack_resolves_the_alert(self):
        project = self.instance.doodba_project_name
        self.executor._check_managed_containers(self._survivors())
        self.assertTrue(self._alert())
        self.executor._check_managed_containers(
            self._survivors(f"{project}-odoo-1", f"{project}-db-1"),
        )
        self.assertFalse(self._alert())

    def test_busy_instance_is_exempt(self):
        """A rebuild's ``compose down`` phase must not false-alarm."""
        Job = self.env["cloud.job"]
        with patch.object(
            type(Job), "search_count", autospec=True, return_value=1,
        ):
            self.executor._check_managed_containers(self._survivors())
        self.assertFalse(self._alert())

    def test_undeployed_instance_is_ignored(self):
        """A draft instance has no containers to demand from the host."""
        # Its own project: a project holds at most one production
        # instance (``cloud_instance_one_production_per_project``).
        draft_project = self.env["cloud.project"].create(
            {"name": "Guard Draft Proj"},
        )
        self.env["cloud.instance"].create({
            "name": "guarddraft",
            "project_id": draft_project.id,
            "environment": "production",
            "host_id": self.host.id,
            # default state 'draft' → deployed is False
        })
        project = self.instance.doodba_project_name
        self.executor._check_managed_containers(
            self._survivors(f"{project}-odoo-1", f"{project}-db-1"),
        )
        self.assertFalse(self._alert())
