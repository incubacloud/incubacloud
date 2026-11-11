"""Both web terminals end their session when the shell ends, not only the tab.

The server keeps one open console per user and host, and refuses the next
one ("You already have an open console on this host"). The pages ended
their session on ``beforeunload`` only: a shell left with ``exit`` stayed
open on the server until its tab was closed or the routes' GC came, and a
second console on that host was refused meanwhile (QA, 19-oct-2026).

Nothing in the Python suite runs the pages' script, so the checks are
textual, as in ``test_terminal_input_order``. Both pages call the same
script, ``terminal_client_script.xml``.
"""
import pathlib

from odoo.tests.common import BaseCase

_VIEWS = pathlib.Path(__file__).resolve().parent.parent / "views"

#: The script both terminal pages call: an instance's containers and a host.
_PAGES = ("terminal_client_script.xml",)


class TestTerminalSessionClose(BaseCase):

    def _function(self, text, name):
        """The body of the page's ``function <name>(...)``, up to its end."""
        start = text.index(f"function {name}(")
        return text[start:text.index("\n            }\n", start)]

    def test_the_session_ends_with_the_shell(self):
        for page in _PAGES:
            with self.subTest(page=page):
                text = (_VIEWS / page).read_text()
                self.assertIn("closeSession();", self._function(text, "onClosed"))

    def test_closing_the_tab_ends_it_too(self):
        for page in _PAGES:
            with self.subTest(page=page):
                text = (_VIEWS / page).read_text()
                self.assertIn(
                    "window.addEventListener('beforeunload', closeSession);", text,
                )

    def test_the_session_is_ended_once_and_outlives_the_tab(self):
        for page in _PAGES:
            with self.subTest(page=page):
                closer = self._function((_VIEWS / page).read_text(), "closeSession")
                self.assertIn("if (sessionClosed) return;", closer)
                self.assertIn("'/close'", closer)
                self.assertIn("keepalive: true", closer)
