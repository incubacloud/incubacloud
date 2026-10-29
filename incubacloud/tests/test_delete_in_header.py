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
        """The bar's last group, its upkeep, ends with the trash."""
        bar = _template("host_detail").xpath(
            "//div[contains(@class, 'ic-action-bar')]"
        )[0]
        upkeep = bar.xpath("div[contains(@class, 'ic-ab-upkeep')]")
        self.assertEqual(len(upkeep), 1)
        trash = upkeep[0].xpath("(.//button)[last()]")[0]
        self.assertIn("ic-ab-delete", trash.get("class"))
        self.assertEqual(trash.get("t-on-click"), "deleteHost")
        self.assertIn("rl-iconbtn danger", trash.get("class"))
        self.assertEqual(trash.get("aria-label"), "Delete host")

    def test_the_host_trash_stays_last_after_extensions(self):
        """Extensions append inside the bar, after the upkeep group."""
        stylesheet = (
            _COMPONENTS / "host_detail" / "host_detail.scss"
        ).read_text()
        self.assertIn(".ic-action-bar > .ic-ab-upkeep {\n    order: 1;", stylesheet)

    def test_the_host_bar_never_leaves_the_trash_alone(self):
        """The bar breaks between its two groups, each kept whole while it fits.

        It wrapped button by button, and at 1440 pixels the trash went to
        a second line on its own (visual pass of the release candidate,
        29-oct-2026).
        """
        bar = _template("host_detail").xpath(
            "//div[contains(@class, 'ic-action-bar')]"
        )[0]
        self.assertEqual(
            [group.get("class") for group in bar.xpath("div")],
            ["ic-ab-group", "ic-ab-group ic-ab-upkeep"],
        )
        self.assertFalse(bar.xpath("button | span"), "every control sits in a group")

    def test_no_host_bar_separator_can_end_a_line(self):
        """A separator sits between buttons kept on one line, never between groups.

        One between two items of a wrapping group ended a line at 768
        pixels ("Sync Direct Path |", visual pass of the release
        candidate, 29-oct-2026). The host key, the shell and the trash
        form a unit that wraps whole, and the separators are inside it.
        """
        bar = _template("host_detail").xpath(
            "//div[contains(@class, 'ic-action-bar')]"
        )[0]
        for group in bar.xpath("div"):
            self.assertFalse(
                group.xpath("*[contains(@class, 'rl-asep')]"),
                "a separator between the items of a wrapping group",
            )
        tail = bar.xpath("div/div[contains(@class, 'ic-ab-tail')]")
        self.assertEqual(len(tail), 1)
        kinds = [
            "sep" if "rl-asep" in child.get("class", "") else "button"
            for child in tail[0].xpath("*")
        ]
        self.assertEqual(kinds, ["button", "sep", "button", "sep", "button"])

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
