"""Both web terminals send what is typed one request at a time, in order.

Each keystroke used to be a request of its own, sent without waiting for
the one before. The server's workers answer requests in parallel, so a
fast typist, a paste split by the browser or a slow link reached the
shell scrambled: a typed ``echo OK-$((40+2))`` reached the shells as
``eco hOK-…`` and ``echo OK-$4(02()+`` (8-oct-2026).

Nothing in the Python suite runs the pages' script and the hoot tests
are not part of any gate, so the checks are textual, as for the action
bar labels.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_VIEWS = pathlib.Path(__file__).resolve().parent.parent / "views"

#: The two terminal pages: an instance's containers and a host.
_PAGES = ("cloud_terminal_page.xml", "host_terminal_page.xml")

#: The body of ``term.onData(...)`` and ``term.onBinary(...)``.
_HANDLER_RE = re.compile(
    r"term\.on(?:Data|Binary)\(function \(data\) \{(.*?)\n\s*\}\);", re.DOTALL
)


class TestTerminalInputOrder(BaseCase):

    def _input_block(self, page):
        """The page's script from its input section to its resize one."""
        text = (_VIEWS / page).read_text()
        start = text.index("// ── Input → server")
        return text[start:text.index("// ── Resize", start)]

    def test_keystrokes_never_go_straight_to_the_server(self):
        for page in _PAGES:
            with self.subTest(page=page):
                handlers = _HANDLER_RE.findall(self._input_block(page))
                self.assertEqual(len(handlers), 2)
                for body in handlers:
                    self.assertNotIn("rpc(", body)
                    self.assertIn("sendInput(", body)

    def test_the_next_request_waits_for_the_one_out(self):
        for page in _PAGES:
            with self.subTest(page=page):
                block = self._input_block(page)
                self.assertEqual(block.count("rpc("), 1)
                self.assertRegex(
                    block,
                    r"rpc\([^;]*'/input'[^;]*\)\s*"
                    r"\.catch\(function \(\) \{\}\)\s*\.then\(flushInput\)",
                )
