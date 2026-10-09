"""A project starts with production; staging waits until there is one.

A customer used to platforms where an instance starts on a development
copy and is promoted later made a staging the only instance of their
project (9-oct-2026). Here a staging is a copy of production: the empty
project and the sidebar offer it only once production exists, and a new
instance form opens on production until then.
"""
import pathlib
from unittest.mock import MagicMock, patch

from lxml import etree

from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud

_COMPONENTS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static" / "src" / "components"
)


class TestStagingNeedsProduction(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "Staging Order Host",
            "ip_address": "10.0.0.31",
            "user": "ubuntu",
            "wildcard_domain": "*.order.example.com",
        })
        self.project = self.env["cloud.project"].create({
            "name": "Staging Order Project",
        })
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]

    def _has_production(self):
        """``has_production`` as ``/cloud/get_project`` reports it.

        :return: the reported value.
        """
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, "request", fake):
            data = self.controller.cloud_get_project(self.project.id)
        return data["has_production"]

    def _instance(self, environment):
        """Add an instance of *environment* to the project.

        :param environment: ``production`` or ``staging``.
        :return: the ``cloud.instance`` record.
        """
        return self.env["cloud.instance"].create({
            "name": f"order-{environment}",
            "project_id": self.project.id,
            "host_id": self.host.id,
            "environment": environment,
        })

    def test_an_empty_project_has_no_production(self):
        self.assertIs(self._has_production(), False)

    def test_a_staging_alone_is_not_a_production(self):
        self._instance("staging")
        self.assertIs(self._has_production(), False)

    def test_a_production_instance_counts(self):
        self._instance("production")
        self.assertIs(self._has_production(), True)

    def test_the_empty_project_offers_staging_as_an_explanation(self):
        tree = etree.parse(
            str(_COMPONENTS / "project_detail" / "project_detail.xml"),
        )
        staging = tree.xpath(
            "//div[contains(@class, 'ic-pd-onboard-card')"
            " and contains(@class, 'env-staging')]"
        )[0]
        self.assertIn("is-disabled", staging.get("class"))
        self.assertIsNone(staging.get("t-on-click"))
        production = tree.xpath(
            "//div[contains(@class, 'ic-pd-onboard-card')"
            " and contains(@class, 'env-production')]"
        )[0]
        self.assertEqual(
            production.get("t-on-click"),
            "() => this.createInstance('production')",
        )

    def test_the_sidebar_holds_staging_back_until_production(self):
        tree = etree.parse(
            str(_COMPONENTS / "project_sidebar" / "project_sidebar.xml"),
        )
        add = tree.xpath("//button[contains(@class, 'rl-ctx-add')]")[0]
        self.assertEqual(
            add.get("t-att-disabled"), "needsProduction(envKey) or null",
        )
        script = (
            _COMPONENTS / "project_sidebar" / "project_sidebar.js"
        ).read_text()
        self.assertIn(
            'environment === "staging" && !this.grouped.production.length',
            script,
        )

    def test_a_new_instance_form_opens_on_production_until_there_is_one(self):
        script = (
            _COMPONENTS / "instance_detail" / "instance_detail.js"
        ).read_text()
        self.assertNotIn('this.props.environment || "staging"', script)
        self.assertIn(
            '(project?.has_production ? "staging" : "production")', script,
        )
