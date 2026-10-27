"""The module is there, with exactly the dependencies the design names."""
from odoo.tests.common import TransactionCase


class TestTheModuleIsInstalled(TransactionCase):

    def test_it_is_installed_on_core_and_auth_oauth(self):
        module = self.env["ir.module.module"].search([("name", "=", "incubacloud_mcp")])
        self.assertEqual(module.state, "installed")
        self.assertEqual(
            sorted(module.dependencies_id.mapped("name")),
            ["auth_oauth", "incubacloud"],
        )
