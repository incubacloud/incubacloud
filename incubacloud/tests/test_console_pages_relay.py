"""The pages that open apart from the panel draw with Relay's console.

The two terminals, the live job log and an instance's log viewer are
standalone pages with their own styles: they kept a grey VS Code theme,
a blue accent and Cascadia Code while the panel moved to Relay, so going
from the panel to any of them changed the ground, the colours and the
type (design system review, 30-oct-2026).

As for the terminal input order, the checks are textual: nothing in the
Python suite runs the pages.
"""
import pathlib

from odoo.tests.common import BaseCase

_VIEWS = pathlib.Path(__file__).resolve().parent.parent / "views"

_PAGES = (
    "cloud_terminal_page.xml",
    "host_terminal_page.xml",
    "log_terminal_page.xml",
    "cloud_instance_logs.xml",
)

#: The VS Code theme's ground, panels and accent, and its typeface.
_OLD_LOOK = ("#1e1e1e", "#2d2d2d", "#60b8e0", "Cascadia Code")


class TestConsolePagesDrawWithRelay(BaseCase):

    def test_no_page_keeps_the_old_theme(self):
        for page in _PAGES:
            text = (_VIEWS / page).read_text()
            for trace in _OLD_LOOK:
                with self.subTest(page=page, trace=trace):
                    self.assertNotIn(trace, text)

    def test_every_page_loads_the_brands_fonts_from_the_module(self):
        for page in _PAGES:
            with self.subTest(page=page):
                text = (_VIEWS / page).read_text()
                self.assertIn('href="/incubacloud/static/fonts/fonts.css"', text)
                self.assertIn("'JetBrains Mono'", text)

    def test_the_terminals_open_once_the_mono_font_is_there(self):
        """xterm measures its cells when it opens: a later font misaligns the grid."""
        for page in ("cloud_terminal_page.xml", "host_terminal_page.xml"):
            with self.subTest(page=page):
                text = (_VIEWS / page).read_text()
                self.assertLess(
                    text.index("document.fonts.load(\"13px 'JetBrains Mono'\")"),
                    text.index("new Terminal("),
                )
