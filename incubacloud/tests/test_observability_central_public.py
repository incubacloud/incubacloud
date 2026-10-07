"""The metrics central published from its own host, opt-in.

With ``metrics_central_public_host`` empty — a central next to its panel,
production's case — nothing changes: the compose file carries no Traefik
label and no extra network, and the deployment writes the docker-bridge
address it always wrote. With a name, vmauth and Grafana are published
through the host's Traefik under it (the same routes as the hand-written
gateway of a co-located central), and the panel and every agent are
pointed at it.
"""
import asyncio
from unittest.mock import MagicMock, patch

import jinja2
import yaml

from odoo.exceptions import ValidationError
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.models.observability_central_executor import (
    ObservabilityCentralExecutor,
)
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)
from odoo.addons.incubacloud.tests.test_observability_wiring import (
    _CENTRAL_PLAYBOOK,
)

PUBLIC = "metrics-qa.example.test"
ACCOUNT_PREFIXES = {"/w/", "/r/", "/lw/", "/lr/"}


def _compose(**variables):
    """The compose file the playbook writes, rendered as Ansible renders it.

    :param variables: the play's variables to override.
    :rtype: str
    """
    play = yaml.safe_load(_CENTRAL_PLAYBOOK.read_text())[0]
    task = next(
        t for t in play["tasks"] if t.get("name") == "Write the central compose file"
    )
    values = dict(play["vars"])
    values.update(
        ic_central_dir="/root/observability-central",
        ic_gateway_bind="172.17.0.1:8428",
        ic_grafana_bind="172.17.0.1:3000",
        ic_grafana_admin_password="pw",
        ic_retention_days=90,
    )
    values.update(variables)
    # Ansible's own defaults: trim_blocks on, lstrip_blocks off.
    return jinja2.Environment(  # nosec B701 — renders YAML as Ansible does, never HTML
        trim_blocks=True, undefined=jinja2.StrictUndefined,
    ).from_string(
        task["ansible.builtin.copy"]["content"],
    ).render(**values)


def _published(tls="acme", sources=("127.0.0.1/32", "198.51.100.7/32")):
    """The compose file of a central published as PUBLIC, parsed."""
    return yaml.safe_load(_compose(
        ic_public_host=PUBLIC,
        ic_public_tls=tls,
        ic_operator_sources=list(sources),
        ic_grafana_frame_ancestors="https://panel.example.test",
    ))


class TestTheComposeFile(BaseCase):
    """What the playbook writes, with and without a public name."""

    def test_without_a_name_nothing_is_published(self):
        rendered = _compose()
        self.assertNotIn("traefik.", rendered)
        self.assertNotIn("inverseproxy_shared", rendered)
        services = yaml.safe_load(rendered)["services"]
        self.assertEqual(services["vmauth"]["ports"], ["172.17.0.1:8428:8428"])
        self.assertNotIn("labels", services["vmauth"])
        self.assertNotIn("networks", yaml.safe_load(rendered))

    def test_the_account_routes_are_exactly_the_four_prefixes(self):
        labels = _published()["services"]["vmauth"]["labels"]
        rule = labels["traefik.http.routers.ic-central-data.rule"]
        self.assertTrue(rule.startswith(f"Host(`{PUBLIC}`) && ("))
        prefixes = {
            part.split("`")[1] for part in rule.split("PathPrefix(")[1:]
        }
        self.assertEqual(prefixes, ACCOUNT_PREFIXES)

    def test_series_deletion_answers_the_named_sources_only(self):
        labels = _published()["services"]["vmauth"]["labels"]
        self.assertIn(
            "PathPrefix(`/admin-d/`)",
            labels["traefik.http.routers.ic-central-delete.rule"],
        )
        self.assertEqual(
            labels["traefik.http.routers.ic-central-delete.middlewares"],
            "ic-central-delete-sources",
        )
        self.assertEqual(
            labels[
                "traefik.http.middlewares.ic-central-delete-sources"
                ".ipwhitelist.sourcerange"
            ],
            "127.0.0.1/32,198.51.100.7/32",
        )

    def test_the_other_operator_routes_answer_the_host_only(self):
        labels = _published()["services"]["vmauth"]["labels"]
        rule = labels["traefik.http.routers.ic-central-operator.rule"]
        self.assertIn("PathPrefix(`/admin-r/`)", rule)
        self.assertIn("PathPrefix(`/gadmin/`)", rule)
        self.assertEqual(
            labels[
                "traefik.http.middlewares.ic-central-host-only"
                ".ipwhitelist.sourcerange"
            ],
            "127.0.0.1/32",
        )
        # Above the account routes, which do not overlap them anyway.
        self.assertGreater(
            int(labels["traefik.http.routers.ic-central-operator.priority"]),
            int(labels["traefik.http.routers.ic-central-data.priority"]),
        )

    def test_grafana_drops_the_header_it_would_trust(self):
        """Without an identity provider Grafana logs in whoever sends it."""
        labels = _published()["services"]["grafana"]["labels"]
        self.assertEqual(
            labels[
                "traefik.http.middlewares.ic-central-grafana-headers"
                ".headers.customrequestheaders.X-WEBAUTH-USER"
            ],
            "",
        )
        self.assertEqual(
            labels[
                "traefik.http.middlewares.ic-central-grafana-headers"
                ".headers.contentsecuritypolicy"
            ],
            "frame-ancestors 'self' https://panel.example.test",
        )
        self.assertIn(
            "PathPrefix(`/grafana/`)",
            labels["traefik.http.routers.ic-central-grafana.rule"],
        )

    def test_every_router_asks_for_a_certificate_or_uses_the_hosts(self):
        acme = _published(tls="acme")
        default = _published(tls="default")
        for service in ("vmauth", "grafana"):
            routers = {
                key.split(".")[3]
                for key in acme["services"][service]["labels"]
                if key.startswith("traefik.http.routers.")
            }
            for router in routers:
                self.assertEqual(
                    acme["services"][service]["labels"][
                        f"traefik.http.routers.{router}.tls.certresolver"
                    ],
                    "letsencrypt",
                )
                self.assertEqual(
                    default["services"][service]["labels"][
                        f"traefik.http.routers.{router}.tls"
                    ],
                    "true",
                )

    def test_both_join_the_traefik_network_and_keep_their_own(self):
        doc = _published()
        self.assertEqual(
            doc["networks"], {"inverseproxy_shared": {"external": True}},
        )
        for service in ("vmauth", "grafana"):
            self.assertEqual(
                doc["services"][service]["networks"],
                ["default", "inverseproxy_shared"],
            )
            self.assertEqual(
                doc["services"][service]["labels"]["traefik.docker.network"],
                "inverseproxy_shared",
            )


class TestTheDeployment(ObservabilityExecutorCase):
    """What the job hands the playbook and writes back."""

    def _deployed(self, gateway="http://172.17.0.1:8428"):
        """Run ``on_success`` as after a deployment that answered."""
        executor = self._make(ObservabilityCentralExecutor)
        executor._facts = {"ic_central_up": "True", "ic_central_gateway": gateway}
        executor._shipped_accounts = []
        asyncio.run(executor.on_success({}))
        self.settings.invalidate_recordset()
        return self.settings

    def test_without_a_name_the_bridge_address_as_before(self):
        self.settings.write({
            "metrics_central_public_host": False,
            "metrics_remote_write_url": False,
            "grafana_base_url": False,
        })
        settings = self._deployed()
        self.assertEqual(settings.metrics_central_url, "http://172.17.0.1:8428/r")
        self.assertEqual(
            settings.metrics_remote_write_url,
            "http://172.17.0.1:8428/w/api/v1/write",
        )
        self.assertFalse(settings.grafana_base_url)
        self.assertEqual(settings.metrics_central_host_id, self.host)

    def test_without_a_name_a_set_write_url_is_kept(self):
        """Production's agents write to its hand-published gateway."""
        self.settings.write({
            "metrics_central_public_host": False,
            "metrics_remote_write_url": "https://metrics.example.com/w/api/v1/write",
        })
        settings = self._deployed()
        self.assertEqual(
            settings.metrics_remote_write_url,
            "https://metrics.example.com/w/api/v1/write",
        )

    def test_with_a_name_the_panel_and_the_agents_use_it(self):
        self.settings.write({
            "metrics_central_public_host": PUBLIC,
            "metrics_remote_write_url": "https://old.example.com/w/api/v1/write",
            "grafana_base_url": False,
        })
        settings = self._deployed()
        self.assertEqual(settings.metrics_central_url, f"https://{PUBLIC}/r")
        self.assertEqual(
            settings.metrics_remote_write_url, f"https://{PUBLIC}/w/api/v1/write",
        )
        self.assertEqual(settings.grafana_base_url, f"https://{PUBLIC}/grafana/")

    def test_with_a_name_a_grafana_url_already_set_is_kept(self):
        """It is the OIDC client's redirect; changing it breaks the login."""
        self.settings.write({
            "metrics_central_public_host": PUBLIC,
            "grafana_base_url": "https://dash.example.com/grafana/",
        })
        self.assertEqual(
            self._deployed().grafana_base_url, "https://dash.example.com/grafana/",
        )

    def test_the_playbook_gets_nothing_to_publish_without_a_name(self):
        self.settings.write({"metrics_central_public_host": False})
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_public_host"], "")
        self.assertEqual(extra["ic_operator_sources"], [])

    def test_the_playbook_gets_the_name_its_tls_and_the_sources(self):
        self.settings.write({
            "metrics_central_public_host": PUBLIC,
            "metrics_central_operator_sources": "198.51.100.7/32\n",
            "grafana_base_url": False,
        })
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_public_host"], PUBLIC)
        # The host is not behind a CDN: a certificate of its own.
        self.assertEqual(extra["ic_public_tls"], "acme")
        self.assertEqual(
            extra["ic_operator_sources"], ["127.0.0.1/32", "198.51.100.7/32"],
        )
        self.assertEqual(extra["ic_grafana_root_url"], f"https://{PUBLIC}/grafana/")


class TestTheSettings(ObservabilityExecutorCase):
    """A name or a range the routes could not use is refused."""

    def test_a_plain_host_name_is_accepted(self):
        self.settings.write({"metrics_central_public_host": PUBLIC})
        self.assertEqual(self.settings.metrics_central_public_host, PUBLIC)

    def test_anything_else_is_refused(self):
        for value in (
            f"https://{PUBLIC}",
            f"{PUBLIC}/grafana",
            "metrics",
            "Metrics.Example.Test",
            "metrics example.test",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.settings.write({"metrics_central_public_host": value})

    def test_operator_sources_must_be_ranges(self):
        with self.assertRaises(ValidationError):
            self.settings.write(
                {"metrics_central_operator_sources": "the panel\n"},
            )


class TestTheSettingsScreen(TransactionCase):
    """Settings → Monitoring reads and writes both fields."""

    def setUp(self):
        """The settings routes, gated as a manager would pass them."""
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env['cloud.security.mixin']
        self.settings = self.env['cloud.settings'].sudo()._get()
        self.settings.write({
            'metrics_central_public_host': False,
            'metrics_central_operator_sources': False,
        })

    def _save(self, **values):
        """Call ``/cloud/save_general_settings`` with *values*."""
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, 'request', fake):
            return self.controller.cloud_save_general_settings(**values)

    def test_both_are_read_back(self):
        self.settings.write({
            'metrics_central_public_host': PUBLIC,
            'metrics_central_operator_sources': '198.51.100.7/32',
        })
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, 'request', fake):
            data = self.controller.cloud_get_general_settings()
        self.assertEqual(data['metrics_central_public_host'], PUBLIC)
        self.assertEqual(
            data['metrics_central_operator_sources'], '198.51.100.7/32',
        )

    def test_the_name_is_saved_trimmed_and_in_lower_case(self):
        answer = self._save(
            metrics_central_public_host='  Metrics-QA.Example.Test ',
            metrics_central_operator_sources=' 198.51.100.7/32\n',
        )
        self.assertEqual(answer, {'ok': True})
        self.assertEqual(self.settings.metrics_central_public_host, PUBLIC)
        self.assertEqual(
            self.settings.metrics_central_operator_sources, '198.51.100.7/32',
        )

    def test_a_url_is_refused_with_the_reason(self):
        answer = self._save(metrics_central_public_host=f'https://{PUBLIC}/')
        self.assertFalse(answer['ok'])
        self.assertIn('no https://', answer['error'])
        self.assertFalse(self.settings.metrics_central_public_host)

    def test_a_caller_that_predates_them_leaves_them_alone(self):
        self.settings.write({'metrics_central_public_host': PUBLIC})
        self._save(job_retention_days=180)
        self.assertEqual(self.settings.metrics_central_public_host, PUBLIC)
