"""Both web terminals send what is typed one request at a time, in order,
and never lose it.

Each keystroke used to be a request of its own, sent without waiting for
the one before. The server's workers answer requests in parallel, so a
fast typist, a paste split by the browser or a slow link reached the
shell scrambled: a typed ``echo OK-$((40+2))`` reached the shells as
``eco hOK-…`` and ``echo OK-$4(02()+`` (8-oct-2026).

Then a request that failed was dropped in silence: with the output read
every 80 ms, typing went over the proxy's per-address limit, the proxy
answered 429 and ``ss -tln | grep ":22 "`` reached the shell as
``s-n| ep2`` (11-nov-2026). A failed batch is now sent again, numbered
so the server applies it once, and the output is read less often.

Nothing in the Python suite runs the pages' script and the hoot tests
are not part of any gate, so the checks are textual, as for the action
bar labels. Both pages call one script, ``terminal_client_script.xml``.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_VIEWS = pathlib.Path(__file__).resolve().parent.parent / "views"

#: The two terminal pages, an instance's containers and a host, and the
#: routes each gives the shared script.
_PAGES = {
    "cloud_terminal_page.xml": "/cloud/terminal/",
    "host_terminal_page.xml": "/cloud/host_terminal/",
}

_SCRIPT = "terminal_client_script.xml"

#: The body of ``term.onData(...)`` and ``term.onBinary(...)``.
_HANDLER_RE = re.compile(
    r"term\.on(?:Data|Binary)\(function \(data\) \{(.*?)\n\s*\}\);", re.DOTALL
)


class TestTerminalInputOrder(BaseCase):

    def _script(self):
        """The shared script's text."""
        return (_VIEWS / _SCRIPT).read_text()

    def _input_block(self):
        """The script from its input section to its size one."""
        text = self._script()
        start = text.index("// ── Input → server")
        return text[start:text.index("// ── Size", start)]

    def _function(self, name):
        """The body of the script's ``function <name>(...)``."""
        text = self._script()
        start = text.index(f"function {name}(")
        return text[start:text.index("\n            }\n", start)]

    def test_both_pages_run_the_shared_script_on_their_routes(self):
        for page, path in _PAGES.items():
            with self.subTest(page=page):
                text = (_VIEWS / page).read_text()
                self.assertIn('t-call="incubacloud.terminal_client_script"', text)
                self.assertIn(f"t-value=\"'{path}'\"", text)
                self.assertNotIn("<script type=\"text/javascript\">", text)

    def test_keystrokes_never_go_straight_to_the_server(self):
        handlers = _HANDLER_RE.findall(self._input_block())
        self.assertEqual(len(handlers), 2)
        for body in handlers:
            self.assertNotIn("rpc(", body)
            self.assertIn("sendInput(", body)

    def test_the_next_request_waits_for_the_one_out(self):
        block = self._input_block()
        self.assertEqual(block.count("rpc("), 1)
        sender = self._function("sendInput")
        self.assertIn("if (!batch && !inputTimer)", sender.replace("&amp;", "&"))

    def test_a_failed_batch_is_sent_again_numbered_never_dropped(self):
        """The bug of 11-nov: ``.catch(function () {})`` threw it away."""
        flush = self._function("flushInput")
        self.assertNotIn(".catch(function () {})", self._input_block())
        # The same batch, its number and the page's stream, go again
        # after the wait the server asks for.
        self.assertIn("seq: sent.seq, stream: inputStream", flush)
        self.assertIn("setTimeout(flushInput, retryDelay(err, inputFailures))", flush)
        # Given up only when the session refuses it, and then said.
        self.assertIn("'Input refused: '", flush)

    def test_an_http_error_is_a_failure_with_its_retry_after(self):
        rpc = self._function("rpc")
        self.assertIn("if (!r.ok)", rpc)
        self.assertIn("r.headers.get('Retry-After')", rpc)
        self.assertIn("if (r.error)", rpc)
        self.assertIn("err.retryAfterMs", self._function("retryDelay"))

    def test_what_never_reached_the_shell_is_said(self):
        self.assertIn("lostInputNote()", self._function("onClosed"))

    def test_output_written_once_whichever_answer_brings_it(self):
        apply = self._function("applyOutput").replace("&lt;", "<")
        self.assertIn("if (chunk.seq <= afterSeq) return;", apply)
        self.assertIn("after_seq: afterSeq", self._input_block())

    def test_the_output_is_not_read_every_80_ms(self):
        text = self._script()
        self.assertNotIn("POLL_MS      = 80", text)
        for name, value in (("POLL_FAST_MS", 100), ("POLL_IDLE_MS", 1000)):
            self.assertRegex(text, rf"var {name} += {value};")

    def test_the_shell_gets_the_page_size_when_it_connects(self):
        """It started at 80 columns until the window was resized."""
        apply = self._function("applyOutput").replace("&amp;", "&")
        self.assertRegex(
            apply, r"if \(result\.connected && !wasConnected\) \{[^}]*sendSize\(\);",
        )
        self.assertIn("sendSize();", self._function("showRecovered"))
