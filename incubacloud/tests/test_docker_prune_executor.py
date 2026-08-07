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
"""
import os

import yaml

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.docker_prune_executor import (
    PROTECT_LABEL,
    DockerPruneExecutor,
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

    def _prune_cmd(self):
        """Return the cmd string of the play's prune task."""
        tasks = [
            task for task in (self._play().get("tasks") or [])
            if "ansible.builtin.command" in task
        ]
        self.assertEqual(
            len(tasks), 1, "the play must run exactly one command task",
        )
        return tasks[0]["ansible.builtin.command"]["cmd"]

    def test_prune_command_excludes_protected_resources(self):
        """The prune itself carries the exclusion filter."""
        cmd = self._prune_cmd()
        self.assertIn("docker system prune -af", cmd)
        self.assertIn('--filter "label!={{ ic_protect_label }}"', cmd)

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
