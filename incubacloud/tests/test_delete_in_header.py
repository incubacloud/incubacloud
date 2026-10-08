"""Hosts, projects and backup destinations offer Delete in their header.

Only an instance did: the others kept Delete at the foot of their form,
under every field, and a host page opens on Overview, which has no
footer at all. A customer could not find how to delete a host
(9-oct-2026).
"""
import pathlib

from lxml import etree

from odoo.tests.common import TransactionCase

_COMPONENTS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static" / "src" / "components"
)


def _template(name):
    """Parse the SPA template of component *name*.

    :param name: the component's directory, which names its ``.xml`` too.
    :return: the parsed template tree.
    """
    return etree.parse(str(_COMPONENTS / name / f"{name}.xml"))


class TestDeleteInHeader(TransactionCase):

    def test_the_host_action_bar_ends_with_a_trash(self):
        bar = _template("host_detail").xpath(
            "//div[contains(@class, 'ic-action-bar')]"
        )[0]
        trash = bar.xpath("button[contains(@class, 'ic-ab-delete')]")
        self.assertEqual(len(trash), 1)
        self.assertEqual(trash[0].get("t-on-click"), "deleteHost")
        self.assertIn("rl-iconbtn danger", trash[0].get("class"))
        self.assertEqual(trash[0].get("aria-label"), "Delete host")

    def test_the_host_trash_stays_last_after_extensions(self):
        """Extensions append inside the bar, after the trash."""
        stylesheet = (
            _COMPONENTS / "host_detail" / "host_detail.scss"
        ).read_text()
        self.assertIn(".ic-action-bar > .ic-ab-delete {", stylesheet)
        self.assertIn(".ic-action-bar > .ic-ab-delete-sep,", stylesheet)

    def test_the_project_header_has_a_trash(self):
        trash = _template("project_detail").xpath(
            "//div[contains(@class, 'rl-phead')]"
            "//button[@t-on-click='deleteProject']"
        )
        self.assertEqual(len(trash), 1)
        self.assertIn("rl-iconbtn danger", trash[0].get("class"))

    def test_the_backup_destination_header_has_a_trash(self):
        trash = _template("backup_backend_detail").xpath(
            "//div[contains(@class, 'rl-phead')]"
            "//button[@t-on-click='deleteBackend']"
        )
        self.assertEqual(len(trash), 1)
        self.assertIn("rl-iconbtn danger", trash[0].get("class"))
