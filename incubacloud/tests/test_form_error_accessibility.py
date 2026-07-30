"""Every field that can show a validation error must announce it.

The red border introduced by ``.has-error`` is a visual-only signal. A
screen reader standing on the control hears nothing: the caption is a
sibling node it never reaches, and the input carries no state. The pair
that fixes it is ``aria-invalid`` on the control plus
``aria-describedby`` pointing at the caption's id.

This is checked structurally rather than in the browser because the
failure mode is an omission — a new field added to a form without the
pair — and a rendering test only covers the forms someone remembered to
write a test for.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_COMPONENTS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static" / "src" / "components"
)


def _error_rows(source):
    """Yield ``(field, row_markup)`` for each validation row in a template.

    :param source: template source.
    :return: iterator of (field name, markup of the row).
    """
    for match in re.finditer(r"has-error", source):
        row_start = source.rfind("<div", 0, match.start())
        caption_at = source.find("ic-field-error", match.start())
        if caption_at < 0:
            continue
        row = source[row_start:source.find("/>", caption_at)]
        field = re.search(r"fieldError\('(\w+)'\)", row)
        if field:
            yield field.group(1), row


class TestValidationRowsAreAnnounced(BaseCase):

    def test_every_error_row_wires_aria(self):
        """Control marked invalid, caption addressable, ids matching."""
        offenders = []
        for template in sorted(_COMPONENTS.glob("*/*.xml")):
            source = template.read_text()
            for field, row in _error_rows(source):
                where = f"{template.parent.name}:{field}"
                # Either a plain control (t-att-aria-*) or RlSelect,
                # which takes the same thing as props.
                invalid = "aria-invalid" in row or "ariaInvalid" in row
                described = (
                    "aria-describedby" in row or "ariaDescribedby" in row
                )
                caption_id = re.search(r't-att-id="\'([\w-]+)\'"', row)
                if not invalid:
                    offenders.append(f"{where}: control not marked invalid")
                if not described:
                    offenders.append(f"{where}: control describes nothing")
                if not caption_id:
                    offenders.append(f"{where}: caption has no id to point at")
                elif caption_id.group(1) not in row:
                    offenders.append(f"{where}: id/describedby mismatch")
        self.assertFalse(
            offenders,
            "validation errors shown only in colour:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_scan_finds_the_rows_it_should(self):
        """Canary: a silent zero here would make the guard vacuous."""
        total = sum(
            len(list(_error_rows(t.read_text())))
            for t in _COMPONENTS.glob("*/*.xml")
        )
        self.assertGreaterEqual(
            total, 15, "the row scan stopped finding validation rows",
        )
