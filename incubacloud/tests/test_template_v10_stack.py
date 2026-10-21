"""A staging rendered by doodba template v10 closes its egress with a gatekeeper.

Template v10 replaced ``proxy_general`` and ``odoo_net_setup`` by one
``gatekeeper`` service: ``odoo`` uses its network (``network_mode:
service:gatekeeper``) and the staging's Traefik labels move onto it. A
stack keeps the model of the template that rendered it until it is
rebuilt, so the panel records that template on the instance
(``stack_template_ref``) and reads the stack from it: what the health
probe expects to run, and where the deploy puts the login routers.
"""
import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.deploy_instance_executor import (
    GATEKEEPER_IMAGE,
    DeployInstanceExecutor,
)


class TestTheStackFollowsItsTemplate(TransactionCase):

    def setUp(self):
        super().setUp()
        project = self.env["cloud.project"].create({"name": "Gatekeeper"})
        self.host = self.env["cloud.host"].create({
            "name": "gk-host", "ip_address": "192.0.2.91", "user": "ubuntu",
            "wildcard_domain": "gk.example.com",
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "gkstag", "project_id": project.id,
            "environment": "staging", "host_id": self.host.id,
        })
        self.env["cloud.instance.whitelist"].create({
            "instance_id": self.staging.id, "hostname": "api.example.com",
            "sequence": 10,
        })
        self.settings = self.env["cloud.settings"].sudo()._get()

    def _override(self, template_ref):
        """The deploy's override for the staging, rendered with *template_ref*."""
        self.settings.copier_template_ref = template_ref
        job = self.env["cloud.job"].create({
            "name": "gk deploy", "host_id": self.host.id,
            "instance_id": self.staging.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "deploy_instance")], limit=1,
            ).id,
        })
        executor = DeployInstanceExecutor(job, self.host)
        return yaml.safe_load(executor._resource_override_content())["services"]

    def test_which_templates_have_a_gatekeeper(self):
        Instance = type(self.staging)
        for ref, expected in (
            ("v10.0.8", True), ("10.1", True), ("v11.0.0", True),
            ("v9.6.1", False), ("", False), ("main", False), (None, False),
        ):
            with self.subTest(ref=ref):
                self.assertIs(Instance._template_has_gatekeeper(ref), expected)

    def test_a_stack_keeps_the_model_of_the_template_that_rendered_it(self):
        """Rendered before the panel recorded it: the sidecars."""
        self.assertFalse(self.staging.stack_template_ref)
        old = self.staging.expected_services()
        self.assertIn("odoo_net_setup", old)
        self.assertNotIn("gatekeeper", old)
        self.staging.stack_template_ref = "v10.0.8"
        new = self.staging.expected_services()
        self.assertIn("gatekeeper", new)
        self.assertNotIn("odoo_net_setup", new)
        self.assertNotIn("proxy_general", new)

    def test_a_staging_without_any_allowlist_has_no_gatekeeper(self):
        """Template v10 renders none without allowed hosts: neither the
        staging's own nor its host's."""
        self.staging.whitelist_ids.unlink()
        self.host.whitelist_ids.unlink()
        self.assertEqual(
            self.staging.expected_services(template_ref="v10.0.8"),
            ("odoo", "db", "smtp"),
        )

    def test_the_deploy_renders_the_stack_of_the_template_it_runs(self):
        services = self._override("v10.0.8")
        self.assertIn("gatekeeper", services)
        self.assertNotIn("odoo_net_setup", services)
        self.assertEqual(services["gatekeeper"]["image"], GATEKEEPER_IMAGE)
        self.assertIn("@sha256:", GATEKEEPER_IMAGE)
        # The login routers go where Traefik reads the staging's labels.
        routers = [k for k in services["gatekeeper"]["labels"] if ".routers." in k]
        self.assertTrue(routers)
        self.assertFalse(
            [k for k in services["odoo"]["labels"] if ".routers." in k],
        )
        self.assertIn("command", services["odoo"])

    def test_the_gatekeeper_comes_back_after_a_host_reboot(self):
        """The template gives it no restart policy, and odoo runs in its
        network: without one, a reboot left the staging down."""
        services = self._override("v10.0.8")
        self.assertEqual(services["gatekeeper"]["restart"], "unless-stopped")
        self.assertEqual(services["gatekeeper"]["image"], GATEKEEPER_IMAGE)

    def test_under_v10_an_empty_list_of_its_own_means_the_hosts(self):
        """Template v10 has no shared whitelist network: a staging without
        an allowlist would reach nothing. The host's list fills the gap,
        the meaning an empty list always had."""
        # The host is born with a seeded list; one entry says it plainly.
        self.host.whitelist_ids.unlink()
        self.env["cloud.host.whitelist"].create({
            "host_id": self.host.id, "hostname": "pypi.org", "sequence": 10,
        })
        self.staging.whitelist_ids.unlink()
        self.assertEqual(self.staging._egress_whitelist("v10.0.8"), ["pypi.org"])
        self.assertIn("gatekeeper", self.staging.expected_services("v10.0.8"))
        # Under the old template the host's proxies answer, as before.
        self.assertEqual(self.staging._egress_whitelist("v9.6.1"), [])
        self.assertNotIn("odoo_net_setup", self.staging.expected_services("v9.6.1"))
        # The deploy renders the pinned template's answer.
        self.settings.copier_template_ref = "v10.0.8"
        answers = self.staging._render_copier_answers()
        self.assertEqual(answers["whitelisted_hosts_test"], ["pypi.org"])
        # Its own list wins when it has one.
        self.env["cloud.instance.whitelist"].create({
            "instance_id": self.staging.id, "hostname": "api.example.com",
            "sequence": 10,
        })
        self.assertEqual(self.staging._egress_whitelist("v10.0.8"), ["api.example.com"])

    def test_a_deploy_on_the_old_template_keeps_the_labels_on_odoo(self):
        services = self._override("v9.6.1")
        self.assertNotIn("gatekeeper", services)
        self.assertIn("odoo_net_setup", services)
        self.assertTrue(
            [k for k in services["odoo"]["labels"] if ".routers." in k],
        )
