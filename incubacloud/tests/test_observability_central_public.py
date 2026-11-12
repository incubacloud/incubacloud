"""The metrics central published from its own host, opt-in.

With ``metrics_central_public_host`` empty — a central next to its panel,
production's case — nothing changes: the compose file carries no Traefik
label and no extra network, and the deployment writes the docker-bridge
address it always wrote. With a name, vmauth is published
through the host's Traefik under it (the same routes as the hand-written
gateway of a co-located central), and the panel and every agent are
pointed at it.
"""
import asyncio
import pathlib
from unittest.mock import MagicMock, patch

import jinja2
import yaml

from odoo.exceptions import ValidationError
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.metrics_acl_sync_executor import (
    MetricsAclSyncExecutor,
)
from odoo.addons.incubacloud.models.observability_central_executor import (
    ObservabilityCentralExecutor,
)
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)
from odoo.addons.incubacloud.tests.test_observability_wiring import (
    _CENTRAL_PLAYBOOK,
)

PUBLIC = "metrics-central.example.test"
ACCOUNT_PREFIXES = {"/w/", "/r/", "/lw/", "/lr/"}
DIRECT = "direct.metrics-central.example.test"
_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "central_compose"
_PUBLIC = {
    "ic_public_host": PUBLIC,
}
_BEFORE = {
    "none": {},
    "public_acme": _PUBLIC | {
        "ic_public_tls": "acme",
        "ic_public_forwarded": False,
        "ic_operator_sources": ["127.0.0.1/32", "198.51.100.7/32"],
    },
    "public_default_forwarded": _PUBLIC | {
        "ic_public_tls": "default",
        "ic_public_forwarded": True,
        "ic_operator_sources": ["127.0.0.1/32"],
    },
}


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
        ic_retention_days=90,
    )
    values.update(variables)
    # Ansible's own defaults: trim_blocks on, lstrip_blocks off.
    return jinja2.Environment(  # nosec B701 — renders YAML as Ansible does, never HTML
        trim_blocks=True, undefined=jinja2.StrictUndefined,
    ).from_string(
        task["ansible.builtin.copy"]["content"],
    ).render(**values)


def _published(
    tls="acme", sources=("127.0.0.1/32", "198.51.100.7/32"), forwarded=False,
):
    """The compose file of a central published as PUBLIC, parsed."""
    return yaml.safe_load(_compose(
        ic_public_host=PUBLIC,
        ic_public_tls=tls,
        ic_public_forwarded=forwarded,
        ic_operator_sources=list(sources),
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

    def test_reached_directly_the_deletion_checks_the_connection(self):
        labels = _published()["services"]["vmauth"]["labels"]
        self.assertFalse(
            [key for key in labels if ".ipstrategy." in key], labels,
        )

    def test_behind_a_cdn_the_deletion_checks_the_forwarded_address(self):
        """The CDN's edge opens the connection, not the panel (10-oct-2026)."""
        labels = _published(tls="default", forwarded=True)["services"]["vmauth"][
            "labels"
        ]
        prefix = "traefik.http.middlewares.ic-central-delete-sources.ipwhitelist"
        self.assertEqual(labels[f"{prefix}.ipstrategy.depth"], "1")
        self.assertEqual(
            labels[f"{prefix}.sourcerange"], "127.0.0.1/32,198.51.100.7/32",
        )

    def test_the_cross_account_read_answers_the_named_sources_only(self):
        """Its own router, behind the deletion's list (Fable M2, 12-nov-2026).

        vmauth serves only the accounts' read endpoints and health there,
        so opening it to the panel's address opens nothing else.
        """
        labels = _published()["services"]["vmauth"]["labels"]
        router = "traefik.http.routers.ic-central-read"
        self.assertEqual(
            labels[f"{router}.rule"], f"Host(`{PUBLIC}`) && PathPrefix(`/admin-r/`)",
        )
        self.assertEqual(labels[f"{router}.middlewares"], "ic-central-delete-sources")
        self.assertEqual(labels[f"{router}.entrypoints"], "https")
        self.assertEqual(labels[f"{router}.service"], "ic-central-gateway")
        self.assertGreater(
            int(labels[f"{router}.priority"]),
            int(labels["traefik.http.routers.ic-central-data.priority"]),
        )

    def test_behind_a_cdn_the_read_checks_the_forwarded_address(self):
        labels = _published(tls="default", forwarded=True)["services"]["vmauth"][
            "labels"
        ]
        self.assertEqual(
            labels["traefik.http.routers.ic-central-read.middlewares"],
            "ic-central-delete-sources",
        )
        self.assertEqual(
            labels[
                "traefik.http.middlewares.ic-central-delete-sources"
                ".ipwhitelist.ipstrategy.depth"
            ],
            "1",
        )

    def test_with_no_source_named_the_read_answers_the_host_only(self):
        """The setting empty: the read reaches no one new, as before."""
        labels = _published(sources=("127.0.0.1/32",))["services"]["vmauth"][
            "labels"
        ]
        self.assertEqual(
            labels[
                "traefik.http.middlewares.ic-central-delete-sources"
                ".ipwhitelist.sourcerange"
            ],
            "127.0.0.1/32",
        )

    def test_grafana_is_neither_run_nor_published(self):
        """Its service, its route under the public name and ``/gadmin/``
        are gone; the panel draws every chart (12-nov-2026)."""
        doc = _published()
        self.assertEqual(set(doc["services"]), {"victoriametrics", "loki", "vmauth"})
        rendered = _compose(ic_public_host=PUBLIC, ic_operator_sources=["127.0.0.1/32"])
        for gone in (
            "grafana/grafana", "    grafana:\n", "/gadmin/", "ic-central-operator",
            "GF_", ":3000",
        ):
            self.assertNotIn(gone, rendered)
        self.assertNotIn("grafana-data", doc["volumes"])

    def test_every_router_asks_for_a_certificate_or_uses_the_hosts(self):
        acme = _published(tls="acme")
        default = _published(tls="default")
        for service in ("vmauth",):
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

    def test_vmauth_joins_the_traefik_network_and_keeps_its_own(self):
        doc = _published()
        self.assertEqual(
            doc["networks"], {"inverseproxy_shared": {"external": True}},
        )
        for service in ("vmauth",):
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
        })
        settings = self._deployed()
        self.assertEqual(settings.metrics_central_url, "http://172.17.0.1:8428/r")
        self.assertEqual(
            settings.metrics_remote_write_url,
            "http://172.17.0.1:8428/w/api/v1/write",
        )
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
        })
        settings = self._deployed()
        self.assertEqual(settings.metrics_central_url, f"https://{PUBLIC}/r")
        self.assertEqual(
            settings.metrics_remote_write_url, f"https://{PUBLIC}/w/api/v1/write",
        )

    def test_the_playbook_gets_nothing_to_publish_without_a_name(self):
        self.settings.write({"metrics_central_public_host": False})
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_public_host"], "")
        self.assertEqual(extra["ic_operator_sources"], [])

    def test_the_playbook_gets_the_name_its_tls_and_the_sources(self):
        self.host.behind_cdn = False
        self.settings.write({
            "metrics_central_public_host": PUBLIC,
            "metrics_central_operator_sources": "198.51.100.7/32\n",
        })
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_public_host"], PUBLIC)
        # The host is not behind a CDN: a certificate of its own, and the
        # deletion route checks the connection itself.
        self.assertEqual(extra["ic_public_tls"], "acme")
        self.assertIs(extra["ic_public_forwarded"], False)
        self.assertEqual(
            extra["ic_operator_sources"], ["127.0.0.1/32", "198.51.100.7/32"],
        )

    def test_behind_a_cdn_the_playbook_is_told_the_name_is_forwarded(self):
        self.settings.write({"metrics_central_public_host": PUBLIC})
        self.host.behind_cdn = True
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertIs(extra["ic_public_forwarded"], True)

    def test_without_a_name_nothing_is_forwarded(self):
        self.settings.write({"metrics_central_public_host": False})
        self.host.behind_cdn = True
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertIs(extra["ic_public_forwarded"], False)


class TestTheSettings(ObservabilityExecutorCase):
    """A name or a range the routes could not use is refused."""

    def test_a_plain_host_name_is_accepted(self):
        self.settings.write({"metrics_central_public_host": PUBLIC})
        self.assertEqual(self.settings.metrics_central_public_host, PUBLIC)

    def test_anything_else_is_refused(self):
        for value in (
            f"https://{PUBLIC}",
            f"{PUBLIC}/r",
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
            metrics_central_public_host='  Metrics-Central.Example.Test ',
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


class TestTheDirectRouter(BaseCase):
    """The central's direct name, on the host's direct entrypoint only."""

    def _labels(self, **extra):
        """Return vmauth's labels in a compose file with the direct name."""
        doc = yaml.safe_load(_compose(ic_direct_host=DIRECT, **extra))
        return doc["services"]["vmauth"]["labels"]

    def test_without_a_direct_name_the_file_is_the_one_before(self):
        for name, extra in _BEFORE.items():
            with self.subTest(variant=name):
                self.assertEqual(
                    _compose(**extra), (_FIXTURES / f"{name}.yml").read_text(),
                )

    def test_the_four_account_prefixes_on_the_direct_entrypoint(self):
        labels = self._labels()
        rule = labels["traefik.http.routers.ic-central-direct.rule"]
        self.assertTrue(rule.startswith(f"Host(`{DIRECT}`) && ("))
        prefixes = {part.split("`")[1] for part in rule.split("PathPrefix(")[1:]}
        self.assertEqual(prefixes, ACCOUNT_PREFIXES)
        router = "traefik.http.routers.ic-central-direct"
        self.assertEqual(labels[f"{router}.entrypoints"], "direct")
        self.assertEqual(labels[f"{router}.service"], "ic-central-gateway")
        self.assertEqual(labels[f"{router}.priority"], "10010")
        self.assertEqual(labels[f"{router}.tls.certresolver"], "direct-dns")
        self.assertEqual(labels[f"{router}.tls.options"], "direct@file")

    def test_no_operator_route_answers_on_the_direct_entrypoint(self):
        for extra in ({}, _BEFORE["public_acme"]):
            labels = self._labels(**extra)
            on_direct = {
                key.split(".")[3] for key, value in labels.items()
                if key.endswith(".entrypoints") and value == "direct"
            }
            self.assertEqual(on_direct, {"ic-central-direct"})

    def test_vmauth_alone_joins_the_traefik_network_for_it(self):
        doc = yaml.safe_load(_compose(ic_direct_host=DIRECT))
        self.assertEqual(set(doc["services"]), {"victoriametrics", "loki", "vmauth"})
        self.assertEqual(
            doc["services"]["vmauth"]["networks"], ["default", "inverseproxy_shared"],
        )
        self.assertEqual(doc["networks"], {"inverseproxy_shared": {"external": True}})


class TestTheDirectDeployment(ObservabilityExecutorCase):
    """What the deployment hands the playbook, refuses and queues."""

    def setUp(self):
        super().setUp()
        self.settings.write({"metrics_central_direct_host": DIRECT})

    def _refusal(self, cls):
        """Run the pre-run checks of *cls* with the job's log write patched."""
        executor = self._make(cls)
        executor.job.instance_id = self.env["cloud.instance"]
        with patch.object(cls, "_write_system_direct", autospec=True):
            executor.pre_run_checks()

    def _deployed(self, gateway="http://172.17.0.1:8428"):
        """Run ``on_success`` as after a deployment that answered."""
        return TestTheDeployment._deployed(self, gateway)

    def test_the_playbook_gets_the_direct_name(self):
        self.host.direct_port_shipped = 9443
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_direct_host"], DIRECT)

    def test_without_a_direct_port_on_its_host_it_is_refused(self):
        with self.assertRaisesRegex(JobRefused, "no direct port applied"):
            self._refusal(ObservabilityCentralExecutor)

    def test_a_public_name_without_full_setup_is_refused(self):
        self.settings.write({"metrics_central_public_host": PUBLIC})
        self.host.traefik_deployed = False
        with self.assertRaisesRegex(JobRefused, "Full Setup has not been run"):
            self._refusal(ObservabilityCentralExecutor)

    def test_a_public_name_with_full_setup_is_not_refused_for_it(self):
        self.settings.write({"metrics_central_public_host": PUBLIC})
        self.settings.metrics_central_direct_host = False
        self.host.traefik_deployed = True
        self._refusal(ObservabilityCentralExecutor)

    def test_the_account_sync_is_never_refused_for_it(self):
        self._refusal(MetricsAclSyncExecutor)

    def test_a_deployment_queues_the_probe_of_its_name(self):
        self.host.direct_port_shipped = 9443
        self._deployed()
        self.assertTrue(self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "sync_direct_path"),
        ]))
