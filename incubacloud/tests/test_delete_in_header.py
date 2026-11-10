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
        """The bar's last group, its upkeep, holds the trash."""
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
        self.assertIn(
            ".ic-action-bar > .ic-ab-upkeep {\n    order: 1;\n    margin-left: auto;",
            stylesheet,
        )

    def test_the_host_bar_is_one_row_of_two_groups(self):
        """The bar breaks between its two groups, never inside the trash's.

        At 1440 pixels the trash once went to a second line on its own
        (visual pass of the release candidate, 29-oct-2026); every control
        sits in a group, the trash alone in the last.
        """
        bar = _template("host_detail").xpath(
            "//div[contains(@class, 'ic-action-bar')]"
        )[0]
        self.assertEqual(
            [group.get("class") for group in bar.xpath("div")],
            ["ic-ab-group", "ic-ab-group ic-ab-upkeep"],
        )
        self.assertFalse(bar.xpath("button | span"), "every control sits in a group")
        self.assertEqual(
            len(bar.xpath("div[contains(@class, 'ic-ab-upkeep')]//button")), 1
        )

    def test_the_host_bar_keeps_the_daily_actions_out_of_the_menu(self):
        """Check, Full Setup, hardening and the shell are in the row; the rest, under More."""
        bar = _template("host_detail").xpath(
            "//div[contains(@class, 'ic-action-bar')]"
        )[0]
        row = ["runCheck", "runFullSetup", "openHardeningModal", "openHostShell"]
        in_menu = bar.xpath(".//IcMenu//button/@t-on-click")
        for handler in row:
            with self.subTest(handler=handler):
                self.assertTrue(bar.xpath(
                    f"div[1]//button[not(ancestor::IcMenu)]"
                    f"[contains(@t-on-click, '{handler}')]"
                ))
                self.assertFalse([c for c in in_menu if handler in c])
        for handler in ("openImportBrowser", "hostAction", "trustHostKey"):
            with self.subTest(handler=handler):
                self.assertTrue([c for c in in_menu if handler in c])
        # A layer may add a button of its own before this one.
        self.assertTrue(
            bar.xpath(".//IcMenu//button[@title='Import existing instance']"),
            "an extension's xpath anchors on the import button",
        )

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
