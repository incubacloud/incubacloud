"""A staging's mail catcher must not be published on the internet.

The copier template wires staging Odoo to an in-stack MailHog
(``SMTP_SERVER: smtplocal``) so a copy of production never mails real
customers — and then gives that container a Traefik router on
``<domain>/smtpfake/``. MailHog asks for a password only when
``/etc/mailhog/auth`` exists, and nothing in this module writes it, so
the whole mailbox of an instance carrying production's data was one URL
away from anyone: password resets, invitations, customer addresses. The
hostname is not a secret either — it comes from the host's wildcard and
Certificate Transparency publishes every certificate issued for it.

The override is the only place that can close it: ``copier update``
regenerates the compose files on every rebuild, so a label written
anywhere else would not survive. Compose merges labels by key and the
override wins, which is why flipping ``traefik.enable`` is enough.

The service reaching the override at all is the other half of the fix:
``expected_services`` used to claim staging ran ``odoo`` and ``db``
only, so ``smtp`` was never emitted here.
"""
import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)


class _CatcherCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Catcher"})
        self.host = self.env["cloud.host"].create({
            "name": "catcher-host",
            "ip_address": "192.0.2.71",
            "user": "ubuntu",
            "wildcard_domain": "catcher.example.com",
        })
        self.prod = self.env["cloud.instance"].create({
            "name": "catcherprod",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "smtp_relay_host": "smtp.example.com",
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "catcherstag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })

    def _executor(self, executor_cls, job_type_code, inst):
        """Build *executor_cls* around a fresh job on *inst*."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", job_type_code)], limit=1,
        )
        self.assertTrue(job_type, f"job type {job_type_code} must exist")
        job = self.env["cloud.job"].create({
            "name": f"Catcher {job_type_code}",
            "host_id": self.host.id,
            "instance_id": inst.id,
            "job_type_id": job_type.id,
        })
        return executor_cls(job, self.host)

    def _override(self, executor):
        raw = executor._resource_override_content()
        self.assertTrue(raw, "override must never be None")
        return raw, yaml.safe_load(raw)


class TestStagingMailCatcherIsNotPublished(_CatcherCase):

    def test_staging_override_carries_the_smtp_service(self):
        """Without this the label below has nowhere to live."""
        _raw, data = self._override(
            self._executor(
                DeployInstanceExecutor, "deploy_instance", self.staging,
            )
        )
        self.assertIn("smtp", data["services"])
        self.assertIn("smtp", self.staging.expected_services())

    def test_deploy_disables_the_traefik_router(self):
        _raw, data = self._override(
            self._executor(
                DeployInstanceExecutor, "deploy_instance", self.staging,
            )
        )
        labels = data["services"]["smtp"]["labels"]
        self.assertEqual(labels.get("traefik.enable"), "false")

    def test_rebuild_disables_it_too(self):
        """``copier update`` rewrites the compose files on every rebuild,
        so the rebuild flavour has to re-ship the label."""
        _raw, data = self._override(
            self._executor(
                RebuildInstanceExecutor, "rebuild_instance", self.staging,
            )
        )
        self.assertEqual(
            data["services"]["smtp"]["labels"].get("traefik.enable"),
            "false",
        )

    def test_value_is_quoted_as_a_string(self):
        """A bare ``false`` is a YAML boolean; Traefik reads labels as
        text and compose must hand it one."""
        raw, _data = self._override(
            self._executor(
                DeployInstanceExecutor, "deploy_instance", self.staging,
            )
        )
        self.assertIn("traefik.enable: 'false'", raw)

    def test_pr_preview_is_covered(self):
        """PR previews are created by the GitHub webhook, not by a human,
        and they carry a clone of production just the same."""
        preview = self.env["cloud.instance"].create({
            "name": "catcherpr",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
            "pr_number": 7,
            "pr_repo": "acme/widgets",
        })
        _raw, data = self._override(
            self._executor(DeployInstanceExecutor, "deploy_instance", preview)
        )
        self.assertEqual(
            data["services"]["smtp"]["labels"].get("traefik.enable"),
            "false",
        )

    def test_the_protect_label_survives_beside_it(self):
        """Both labels live in the same mapping; one must not drop the
        other, or ``docker system prune`` reclaims the container."""
        _raw, data = self._override(
            self._executor(
                DeployInstanceExecutor, "deploy_instance", self.staging,
            )
        )
        labels = data["services"]["smtp"]["labels"]
        self.assertEqual(labels.get("traefik.enable"), "false")
        self.assertEqual(labels.get("incubacloud.protect"), "1")

    def test_staging_smtp_is_rotated_and_limited_like_any_service(self):
        self.staging.write({"smtp_memory_limit": "256m", "smtp_cpus": 0.5})
        _raw, data = self._override(
            self._executor(
                DeployInstanceExecutor, "deploy_instance", self.staging,
            )
        )
        smtp = data["services"]["smtp"]
        self.assertEqual(smtp["mem_limit"], "256m")
        self.assertEqual(smtp["cpus"], 0.5)
        self.assertTrue(smtp.get("logging"))

    def test_staging_smtp_gets_no_update_check_switch(self):
        """``ENABLE_UPDATE_CHECK`` belongs to docker-mailserver, which is
        what production relays through. MailHog is a different image and
        has nothing to switch off."""
        _raw, data = self._override(
            self._executor(
                DeployInstanceExecutor, "deploy_instance", self.staging,
            )
        )
        self.assertNotIn("environment", data["services"]["smtp"])


class TestProductionMailIsUntouched(_CatcherCase):

    def test_production_smtp_keeps_its_router(self):
        """Production's ``smtp`` is the real relay behind a domain; the
        label would take it off Traefik."""
        _raw, data = self._override(
            self._executor(DeployInstanceExecutor, "deploy_instance", self.prod)
        )
        labels = data["services"]["smtp"]["labels"]
        self.assertNotIn("traefik.enable", labels)

    def test_no_production_service_is_disabled(self):
        _raw, data = self._override(
            self._executor(DeployInstanceExecutor, "deploy_instance", self.prod)
        )
        for svc, entry in data["services"].items():
            self.assertNotIn(
                "traefik.enable", entry.get("labels", {}),
                f"service {svc!r} must keep its Traefik registration",
            )
