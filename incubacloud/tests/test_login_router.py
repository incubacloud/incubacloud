"""Each instance gets a login router with the host's strict login limit.

The proxy's general limit is the same for every path (300 a minute per
client), which lets one address try 300 passwords a minute, and Odoo's
own cool-down counts per worker. The login router matches only POSTs to
the endpoints that check a password, puts ``loginlimit@file`` first and
otherwise repeats what the template gives its ``/web`` router -- so the
login is never less protected than the rest of the site. Its priority
stays below 30, where routers set in front of an instance's paths for
other reasons (parking, refusing) must keep winning.
"""
import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.deploy_instance_executor import (
    LOGIN_ROUTER_PRIORITY,
    DeployInstanceExecutor,
)

from ._template import pin_template


class TestLoginRouter(TransactionCase):

    def setUp(self):
        super().setUp()
        # The old template's model; template v10's is test_template_v10_stack.
        pin_template(self.env)
        self.project = self.env["cloud.project"].create({"name": "Login Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "login-host",
            "ip_address": "192.0.2.64",
            "user": "ubuntu",
            "wildcard_domain": "login.example.com",
        })

    def _instance(self, environment="production", domains=()):
        inst = self.env["cloud.instance"].create({
            "name": f"logininst{environment[:4]}",
            "project_id": self.project.id,
            "environment": environment,
            "host_id": self.host.id,
        })
        for hostname, extra in domains:
            self.env["cloud.instance.domain"].create(
                {"instance_id": inst.id, "hostname": hostname} | extra,
            )
        return inst

    def _labels(self, inst):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "deploy_instance")], limit=1,
        )
        job = self.env["cloud.job"].create({
            "name": "Login deploy",
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
        """Index of *hostname*'s group: an instance is also born with a
        domain of its own, so the order is the answers' order."""
        return [g["hosts"][0] for g in answers[env]].index(hostname)

    def test_a_production_domain_gets_a_router_and_its_tls_twin(self):
        inst = self._instance(domains=[
            ("erp.client.example", {"cert_resolver": "letsencrypt"}),
        ])
        labels, answers = self._labels(inst)
        key = self._key(answers, "prod")
        i = self._index(answers, "erp.client.example")
        for name in (f"{key}-login-{i}", f"{key}-login-secure-{i}"):
            router = f"traefik.http.routers.{name}"
            self.assertEqual(labels[f"{router}.service"], f"{key}-main")
            self.assertEqual(
                labels[f"{router}.priority"], str(LOGIN_ROUTER_PRIORITY),
            )
            rule = labels[f"{router}.rule"]
            self.assertTrue(rule.startswith(
                "Host(`erp.client.example`) && Method(`POST`) && (",
            ))
            for path in (
                "/web/login", "/web/login/totp", "/web/session/authenticate",
                "/web/reset_password", "/xmlrpc/2/common",
            ):
                self.assertIn(f"Path(`{path}`)", rule)
            self.assertEqual(
                labels[f"{router}.middlewares"],
                f"loginlimit@file, {key}-addSTS, {key}-buffering, "
                f"{key}-compress, {key}-forbid-crawlers, {key}-forceSecure",
            )
        secure = f"traefik.http.routers.{key}-login-secure-{i}"
        self.assertEqual(labels[f"{secure}.tls"], "true")
        self.assertEqual(labels[f"{secure}.tls.certResolver"], "letsencrypt")
        self.assertNotIn(f"traefik.http.routers.{key}-login-{i}.tls", labels)

    def test_it_stays_below_routers_that_park_or_refuse(self):
        """30 and above belong to routers put in front of an instance's
        paths for their own reasons; the template's /web router is 10."""
        self.assertGreater(LOGIN_ROUTER_PRIORITY, 10)
        self.assertLess(LOGIN_ROUTER_PRIORITY, 30)

    def test_a_redirect_gets_no_login_router(self):
        inst = self._instance(domains=[
            ("erp.client.example", {}),
            ("old.client.example", {"redirect_to": "erp.client.example"}),
        ])
        labels, answers = self._labels(inst)
        key = self._key(answers, "prod")
        served = self._index(answers, "erp.client.example")
        moved = self._index(answers, "old.client.example")
        self.assertIn(f"traefik.http.routers.{key}-login-{served}.rule", labels)
        self.assertFalse([
            l for l in labels
            if f"{key}-login-{moved}." in l or f"{key}-login-secure-{moved}." in l
        ])

    def test_a_staging_uses_the_test_key(self):
        inst = self._instance(environment="staging", domains=[
            ("erp-staging.client.example", {}),
        ])
        labels, answers = self._labels(inst)
        key = self._key(answers, "test")
        i = self._index(answers, "erp-staging.client.example", "domains_test")
        self.assertIn(f"traefik.http.routers.{key}-login-{i}.rule", labels)

    def test_one_pair_per_domain_that_serves(self):
        """Every group that serves pages gets its router; none other."""
        inst = self._instance(domains=[("erp.client.example", {})])
        labels, answers = self._labels(inst)
        serving = [g for g in answers["domains_prod"] if not g.get("redirect_to")]
        rules = [l for l in labels if "-login-" in l and l.endswith(".rule")]
        expected = sum(2 if g.get("cert_resolver") else 1 for g in serving)
        self.assertEqual(len(rules), expected)


class TestLoginLimitMiddleware(TransactionCase):
    """The host defines ``loginlimit``, and keys it on the real client
    behind a CDN like the general limit."""

    _CONFIG = (
        "http:\n  middlewares:\n    hsts:\n      headers:\n"
        "        stsSeconds: 31536000\n"
    )

    def setUp(self):
        super().setUp()
        self.Host = type(self.env["cloud.host"])

    def test_the_retrofit_adds_it_once(self):
        out = self.Host._add_traefik_loginlimit_middleware(self._CONFIG)
        self.assertIn("    loginlimit:\n      rateLimit:\n        average: 20\n", out)
        self.assertEqual(self.Host._add_traefik_loginlimit_middleware(out), out)

    def test_behind_a_cdn_it_keys_on_the_forwarded_client(self):
        config = self.Host._add_traefik_loginlimit_middleware(
            self.Host._add_traefik_ratelimit_middleware(self._CONFIG),
        )
        patched = self.Host._patch_config_yml_trusted_proxies(
            config, ["203.0.113.0/24"], False, True,
        )
        loginlimit = patched.split("loginlimit:")[1].split("ratelimit:")[0]
        self.assertIn("ipStrategy", loginlimit)
        self.assertIn("depth: 1", loginlimit)
        self.assertIn("ipStrategy", patched.split("ratelimit:")[1])
