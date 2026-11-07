"""The exception to the edge guard: an instance that serves /website/info.

Every host's proxy refuses the page (priority 2000). An instance with
``website_info_public`` carries, in its compose labels, a router for the
same paths just above it (2010) to its own Odoo, repeating what the
template gives its ``forbiddenCrawlers`` router. Off, the default, the
override and the config snapshot are what they were before the switch
existed; on or off again, the instance has changes a rebuild applies.
"""
import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_host_edge_guard import (
    WEBSITE_INFO_EXCEPTION_PRIORITY,
    WEBSITE_INFO_RULE,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)

from ._template import pin_template


class TestWebsiteInfoRouter(TransactionCase):

    def setUp(self):
        super().setUp()
        pin_template(self.env)
        self.project = self.env["cloud.project"].create({"name": "Info Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "info-host",
            "ip_address": "192.0.2.65",
            "user": "ubuntu",
            "wildcard_domain": "info.example.com",
        })

    def _instance(self, environment="production", domains=(), public=False):
        inst = self.env["cloud.instance"].create({
            "name": f"infoinst{environment[:4]}",
            "project_id": self.project.id,
            "environment": environment,
            "host_id": self.host.id,
        })
        for hostname, extra in domains:
            self.env["cloud.instance.domain"].create(
                {"instance_id": inst.id, "hostname": hostname} | extra,
            )
        if public:
            inst.website_info_public = True
        return inst

    def _labels(self, inst):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "deploy_instance")], limit=1,
        )
        job = self.env["cloud.job"].create({
            "name": "Info deploy",
            "host_id": self.host.id,
            "instance_id": inst.id,
            "job_type_id": job_type.id,
        })
        executor = DeployInstanceExecutor(job, self.host)
        override = yaml.safe_load(executor._resource_override_content())
        return override["services"]["odoo"]["labels"], inst._render_copier_answers()

    def _key(self, answers, suffix):
        return (
            f"{answers['project_name']}-{float(answers['odoo_version']):.1f}-{suffix}"
        ).replace(".", "-")

    @staticmethod
    def _index(answers, hostname, env="domains_prod"):
        return [g["hosts"][0] for g in answers[env]].index(hostname)

    def test_off_by_default_no_router_and_the_snapshot_as_before(self):
        inst = self._instance(domains=[
            ("erp.client.example", {"cert_resolver": "letsencrypt"}),
        ])
        self.assertFalse(inst.website_info_public)
        labels, _answers = self._labels(inst)
        self.assertFalse([label for label in labels if "-website-info-" in label])
        snapshot = inst._render_config_snapshot()
        self.assertEqual(set(snapshot), {"answers", "repos", "extras"})
        self.assertNotIn("website_info_public", snapshot["extras"])

    def test_on_a_router_and_its_tls_twin_above_the_guard(self):
        inst = self._instance(
            domains=[("erp.client.example", {"cert_resolver": "letsencrypt"})],
            public=True,
        )
        labels, answers = self._labels(inst)
        key = self._key(answers, "prod")
        i = self._index(answers, "erp.client.example")
        for name in (f"{key}-website-info-{i}", f"{key}-website-info-secure-{i}"):
            router = f"traefik.http.routers.{name}"
            self.assertEqual(
                labels[f"{router}.rule"],
                f"Host(`erp.client.example`) && ({WEBSITE_INFO_RULE})",
            )
            self.assertEqual(labels[f"{router}.service"], f"{key}-main")
            self.assertEqual(
                labels[f"{router}.priority"], str(WEBSITE_INFO_EXCEPTION_PRIORITY),
            )
            self.assertEqual(
                labels[f"{router}.middlewares"],
                f"{key}-addSTS, {key}-buffering, {key}-compress, "
                f"{key}-forbid-crawlers, {key}-forceSecure",
            )
        secure = f"traefik.http.routers.{key}-website-info-secure-{i}"
        self.assertEqual(labels[f"{secure}.tls"], "true")
        self.assertEqual(labels[f"{secure}.tls.certResolver"], "letsencrypt")
        self.assertNotIn(f"traefik.http.routers.{key}-website-info-{i}.tls", labels)

    def test_a_redirect_gets_no_router(self):
        inst = self._instance(domains=[
            ("erp.client.example", {}),
            ("old.client.example", {"redirect_to": "erp.client.example"}),
        ], public=True)
        labels, answers = self._labels(inst)
        key = self._key(answers, "prod")
        served = self._index(answers, "erp.client.example")
        moved = self._index(answers, "old.client.example")
        self.assertIn(f"traefik.http.routers.{key}-website-info-{served}.rule", labels)
        self.assertFalse([
            label for label in labels
            if f"{key}-website-info-{moved}." in label
            or f"{key}-website-info-secure-{moved}." in label
        ])

    def test_one_router_per_group_that_serves(self):
        inst = self._instance(domains=[("erp.client.example", {})], public=True)
        labels, answers = self._labels(inst)
        serving = [g for g in answers["domains_prod"] if not g.get("redirect_to")]
        rules = [
            label for label in labels
            if "-website-info-" in label and label.endswith(".rule")
        ]
        self.assertEqual(
            len(rules), sum(2 if g.get("cert_resolver") else 1 for g in serving),
        )

    def test_a_staging_uses_the_test_key(self):
        inst = self._instance(environment="staging", domains=[
            ("erp-staging.client.example", {}),
        ], public=True)
        labels, answers = self._labels(inst)
        key = self._key(answers, "test")
        i = self._index(answers, "erp-staging.client.example", "domains_test")
        self.assertIn(f"traefik.http.routers.{key}-website-info-{i}.rule", labels)

    def test_switching_it_leaves_changes_for_a_rebuild(self):
        inst = self._instance(domains=[("erp.client.example", {})])
        blocked = inst._config_snapshot_hash()
        inst.website_info_public = True
        self.assertTrue(inst._render_config_snapshot()["website_info_public"])
        self.assertNotEqual(inst._config_snapshot_hash(), blocked)
        inst.website_info_public = False
        self.assertEqual(inst._config_snapshot_hash(), blocked)

    def test_a_copy_starts_blocked(self):
        self.assertFalse(
            self.env["cloud.instance"]._fields["website_info_public"].copy,
        )
        self.assertIn(
            "website_info_public", self.env["cloud.instance"]._AUDIT_TRACKED_FIELDS,
        )
