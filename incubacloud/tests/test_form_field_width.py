"""Every field is as wide as its column, and the column is the form.

Three widths of field used to share one screen — a select at 575px, a
number at 120px, a textarea at 100% — and none of them was the width of
the form. It read as carelessness, and it is the first thing a partner
sees. The cause was not design but four CSS rules fighting: a 460px cap
per row, inline widths on the numbers, two-column grids holding a single
field, and a width rule that reached the checkboxes too.

The base rules are fixed in ``relay.scss``. What a stylesheet cannot
stop is the next template reintroducing one of the two shapes by hand,
which is what these check — structurally, with no browser, because a
layout regression that needs a screenshot to see is a layout regression
nobody will see.
"""
import os
import re
import xml.etree.ElementTree as ET

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase

#: Addons whose SPA templates share ``relay.scss`` and therefore its rules.
_ADDONS = (
    "incubacloud",
    "incubacloud_saas_manager",
    "incubacloud_tenant",
)

#: Form controls a width belongs to the stylesheet for.
_FIELD_TAGS = {"input", "select", "textarea"}

#: ``width`` inside a ``style`` attribute, ignoring ``min-width`` and
#: ``max-width`` on layout wrappers, which are a different question.
_WIDTH_RE = re.compile(r"(?<![a-z-])width\s*:", re.IGNORECASE)


def _template_files():
    """Yield every SPA template of every addon that shares the rules."""
    for addon in _ADDONS:
        try:
            root = get_module_path(addon)
        except Exception:
            continue
        if not root:
            continue
        static = os.path.join(root, "static", "src")
        if not os.path.isdir(static):
            continue
        for dirpath, _dirnames, filenames in os.walk(static):
            for name in filenames:
                if name.endswith(".xml"):
                    yield addon, os.path.join(dirpath, name)


def _iter_elements(path):
    """Yield every element of *path*, or nothing if it will not parse.

    Parsing is not this test's job: a malformed template fails loudly
    when the assets are built, and swallowing the error here would be
    the same silence this file exists to remove — but failing here on
    it would also blame the wrong test.
    """
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []
    return tree.iter()


class TestNoInlineFieldWidths(BaseCase):
    """A width on a field is a width that disagrees with its neighbours."""

    def test_no_form_control_carries_an_inline_width(self):
        offenders = []
        for addon, path in _template_files():
            for el in _iter_elements(path):
                if el.tag not in _FIELD_TAGS:
                    continue
                style = el.get("style") or ""
                if _WIDTH_RE.search(style):
                    offenders.append(
                        f"{addon}:{os.path.basename(path)} <{el.tag} "
                        f"style=\"{style}\">"
                    )
        self.assertFalse(
            offenders,
            "Form controls with a hand-set width: "
            + "; ".join(offenders)
            + ". A field takes the width of its column; pair two fields "
            "in an .rl-fgrid if one should be narrower.",
        )


class TestNoSingleChildGrid(BaseCase):
    """A two-column grid holding one field leaves a hole beside it."""

    def test_no_rl_fgrid_wraps_a_single_row(self):
        offenders = []
        for addon, path in _template_files():
            for el in _iter_elements(path):
                classes = (el.get("class") or "").split()
                if "rl-fgrid" not in classes:
                    continue
                children = list(el)
                if len(children) != 1:
                    continue
                child_classes = (children[0].get("class") or "").split()
                if "rl-frow" in child_classes:
                    offenders.append(
                        f"{addon}:{os.path.basename(path)} "
                        f"(.rl-fgrid around one .rl-frow)"
                    )
        self.assertFalse(
            offenders,
            "Two-column grids holding a single field: "
            + "; ".join(offenders)
            + ". One child means it was never a pair — use .rl-frow on "
            "its own and the field fills the form.",
        )


class TestTheBaseRulesStayFixed(BaseCase):
    """The stylesheet is where the two fixes live; keep them there."""

    def _relay(self):
        path = os.path.join(
            get_module_path("incubacloud"), "static", "src", "scss",
            "relay.scss",
        )
        with open(path) as handle:
            return handle.read()

    def test_rl_frow_has_no_width_cap(self):
        """A cap here is what made a field narrower than its form."""
        block = self._relay().split(".rl-frow {", 1)[1].split("\n", 1)[0]
        self.assertNotIn("max-inline-size", block)
        self.assertNotIn("max-width", block)

    def test_the_width_rule_spares_checkboxes(self):
        """Stretched to 100%, a checkbox renders as an empty text box."""
        relay = self._relay()
        self.assertIn(
            "input:not([type=checkbox]):not([type=radio])", relay,
            "the .rl-frow width rule must exclude checkboxes and radios",
        )
