"""Version helpers retained by the API-only project importer."""

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_github as rg,
)
from odoo.addons.incubacloud.models._odoo_versions import ODOO_VERSIONS


class TestImportVersionHelpers(BaseCase):
    """Pure helpers used while interpreting remote GitHub blobs."""

    def test_to_https_from_ssh(self):
        """SCP-style GitHub URLs are rewritten to canonical HTTPS."""
        self.assertEqual(
            rg._to_https("git@github.com:OCA/partner-contact.git"),
            "https://github.com/OCA/partner-contact.git",
        )

    def test_to_https_passthrough(self):
        """An already-HTTPS URL is normalized without changing its repo."""
        self.assertEqual(
            rg._to_https("https://github.com/OCA/x.git"),
            "https://github.com/OCA/x.git",
        )

    def test_version_from_manifest_ok(self):
        """The Odoo series is the first two segments of ``version``."""
        self.assertEqual(
            rg._version_from_manifest_text("{'version': '18.0.1.0.11'}"),
            "18.0",
        )

    def test_version_from_manifest_unsupported(self):
        """An unsupported series yields the empty string."""
        self.assertEqual(
            rg._version_from_manifest_text("{'version': '99.0.1.0.0'}"),
            "",
        )

    def test_version_from_manifest_garbage(self):
        """Unparseable manifest text never raises."""
        self.assertEqual(rg._version_from_manifest_text("not <<< a dict"), "")


class TestOdooVersionSingleSource(TransactionCase):
    """The version selectors and importer share one source of truth."""

    def test_models_share_constant(self):
        """Project and instance selections equal ``ODOO_VERSIONS``."""
        instance_versions = [
            code for code, _label
            in self.env['cloud.instance']._fields['odoo_version'].selection
        ]
        project_versions = [
            code for code, _label
            in self.env['cloud.project']._fields['odoo_version'].selection
        ]
        self.assertEqual(instance_versions, list(ODOO_VERSIONS))
        self.assertEqual(project_versions, list(ODOO_VERSIONS))
