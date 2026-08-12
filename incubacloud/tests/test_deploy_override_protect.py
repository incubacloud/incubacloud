"""The docker-compose override must always carry the protect label.

``docker system prune -af --filter "label!=incubacloud.protect=1"``
deletes any *unlabelled* stopped resource, and panel-managed stacks sit
stopped on purpose (warm spares, Sablier-slept tenants, manual stops).
The label therefore has to be stamped by the **base** deploy executor,
unconditionally — the first design put it in a SaaS subclass and every
rebuild whose MRO skipped that subclass silently unlabelled the stack,
which the nightly prune then swept (2026-08 incident).
"""
import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.docker_prune_executor import PROTECT_LABEL
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)


class TestDeployOverrideProtectLabel(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Protect Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "protect-host",
            "ip_address": "192.0.2.61",
            "user": "ubuntu",
            "wildcard_domain": "protect.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "protectinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.key, self.value = PROTECT_LABEL.split("=", 1)

    def _executor(self, executor_cls, job_type_code, inst=None):
        """Build *executor_cls* around a fresh job on *inst*."""
        inst = inst or self.instance
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", job_type_code)], limit=1,
        )
        self.assertTrue(job_type, f"job type {job_type_code} must exist")
        job = self.env["cloud.job"].create({
            "name": f"Protect {job_type_code}",
            "host_id": self.host.id,
            "instance_id": inst.id,
            "job_type_id": job_type.id,
        })
        return executor_cls(job, self.host)

    def _override(self, executor):
        raw = executor._resource_override_content()
        self.assertTrue(raw, "override must never be None anymore")
        return yaml.safe_load(raw)

    def _assert_protected(self, data, services):
        for svc in services:
            labels = data["services"].get(svc, {}).get("labels") or {}
            self.assertEqual(
                labels.get(self.key), self.value,
                f"service {svc!r} must carry the protect label",
            )
        net_labels = (
            data.get("networks", {}).get("default", {}).get("labels") or {}
        )
        self.assertEqual(
            net_labels.get(self.key), self.value,
            "the default network must carry the protect label",
        )

    def test_production_override_protects_all_expected_services(self):
        """No resource limits set: the label alone justifies the file."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        data = self._override(executor)
        expected = self.instance.expected_services()
        self.assertEqual(set(data["services"]), set(expected))
        self._assert_protected(data, expected)

    def test_limits_survive_alongside_the_label(self):
        self.instance.write({"odoo_memory_limit": "1g", "odoo_cpus": 2.0})
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        data = self._override(executor)
        odoo = data["services"]["odoo"]
        self.assertEqual(odoo["mem_limit"], "1g")
        self.assertEqual(odoo["cpus"], 2.0)
        self._assert_protected(data, self.instance.expected_services())

    def test_staging_override_protects_test_services(self):
        staging = self.env["cloud.instance"].create({
            "name": "protectstag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        executor = self._executor(
            DeployInstanceExecutor, "deploy_instance", inst=staging,
        )
        data = self._override(executor)
        self.assertEqual(
            set(data["services"]),
            set(DeployInstanceExecutor._TEST_SERVICES),
        )
        self._assert_protected(data, DeployInstanceExecutor._TEST_SERVICES)

    def test_rebuild_inherits_the_label(self):
        """The 2026-08 incident: the rebuild flavour lost the label."""
        executor = self._executor(RebuildInstanceExecutor, "rebuild_instance")
        data = self._override(executor)
        self._assert_protected(data, self.instance.expected_services())
