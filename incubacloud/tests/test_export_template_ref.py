"""The export test renders the template the panel deploys from.

``tests/shell/export_starts.bats`` renders the doodba template at the
version written in its fixture. When the panel's pin moves, that fixture
must move with it, or the test goes on proving a template no instance is
built from.
"""
from odoo.tests.common import TransactionCase
from odoo.tools.misc import file_path


class TestExportTemplateRef(TransactionCase):

    def test_the_export_test_renders_the_pinned_template(self):
        with open(
            file_path("incubacloud/tests/shell/fixtures/doodba_template_ref"),
            encoding="utf-8",
        ) as fixture:
            rendered = fixture.read().strip()
        pinned = self.env["cloud.settings"].default_get(["copier_template_ref"])
        self.assertEqual(rendered, pinned["copier_template_ref"])
