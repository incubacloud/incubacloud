"""``ChromeStartRetry``: one more Chrome only when the test had not begun."""
from unittest.mock import patch

from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.tests._browser import ChromeStartRetry

_HUNG = TimeoutError("Network.setCookie({'name': 'session_id', 'value': 'x'})")


class TestChromeStartRetry(HttpCase):

    def _run(self, *outcomes):
        """Call the mixin's ``browser_js`` over Odoo's, answering *outcomes*.

        :return: ``(result or exception, number of calls to Odoo's)``
        """
        with patch.object(
            HttpCase, "browser_js", autospec=True, side_effect=list(outcomes),
        ) as odoo_browser_js:
            try:
                # Built here, not at module level: the loader would collect
                # a module-level case and run it.
                probe = type("Probe", (ChromeStartRetry, HttpCase), {
                    "runTest": lambda self: None,
                })()
                result = probe.browser_js("/web", "code", login="admin")
            except TimeoutError as exc:
                result = exc
        return result, odoo_browser_js.call_count

    def test_a_chrome_that_hung_at_the_start_gets_one_more_run(self):
        with self.assertLogs("odoo.addons.incubacloud.tests._browser", "WARNING"):
            result, calls = self._run(_HUNG, "ok")
        self.assertEqual((result, calls), ("ok", 2))

    def test_a_page_chrome_never_started_opening_gets_one_more_run(self):
        hung = TimeoutError("Page.navigate({'url': 'http://127.0.0.1:8069/cloud'})")
        with self.assertLogs("odoo.addons.incubacloud.tests._browser", "WARNING"):
            result, calls = self._run(hung, "ok")
        self.assertEqual((result, calls), ("ok", 2))

    def test_it_is_only_once(self):
        with self.assertLogs("odoo.addons.incubacloud.tests._browser", "WARNING"):
            result, calls = self._run(_HUNG, _HUNG)
        self.assertIsInstance(result, TimeoutError)
        self.assertEqual(calls, 2)

    def test_a_timeout_once_the_page_is_open_is_the_test_s_own(self):
        result, calls = self._run(TimeoutError("Runtime.evaluate({...})"))
        self.assertIsInstance(result, TimeoutError)
        self.assertEqual(calls, 1)
