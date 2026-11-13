"""``ChromeStartRetry``: one more Chrome only when the test had not begun.

Odoo's own ``browser_js`` runs here for real; only Chrome is simulated,
built from Odoo's ``ChromeBrowser`` so each step answers as told.
"""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from odoo.tests import common, tagged
from odoo.tests.common import ChromeBrowser, ChromeBrowserException, HttpCase, NoScreencast

from odoo.addons.incubacloud.tests._browser import ChromeStartRetry

_LOGGER = "odoo.addons.incubacloud.tests._browser"


def _hung(method):
    """The timeout Odoo raises when Chrome never answers *method*.

    :param method: the DevTools method, e.g. ``Network.setCookie``.
    :return: a ``TimeoutError`` worded as Odoo's ``_websocket_request``.
    """
    return TimeoutError(f"{method}({{'name': 'session_id', 'value': 'x'}})")


@tagged("post_install", "-at_install")
class TestChromeStartRetry(ChromeStartRetry, HttpCase):

    def _chrome(self, **answers):
        """A simulated Chrome with Odoo's ``ChromeBrowser`` API.

        :param answers: side effects by method name, e.g.
            ``set_cookie=[_hung("Network.setCookie")]``.
        :return: a ``MagicMock(spec=ChromeBrowser)`` with the two objects
            Odoo's ``browser_js`` enters: its cleanup and screencaster.
        """
        chrome = MagicMock(spec=ChromeBrowser)
        chrome.cleanup = ExitStack()
        chrome.screencaster = MagicMock(spec=NoScreencast)
        chrome.stop.side_effect = chrome.cleanup.close
        for name, effect in answers.items():
            getattr(chrome, name).side_effect = effect
        return chrome

    def _run(self, *chromes):
        """Call ``browser_js``, Odoo's under the mixin, building *chromes* in turn.

        :return: ``(what it raised or None, number of Chromes built)``.
        """
        built = MagicMock(spec=ChromeBrowser, side_effect=list(chromes))
        with patch.object(common, "ChromeBrowser", built):
            try:
                self.browser_js("/web/login", "", login=None)
            except (TimeoutError, self.failureException) as exc:
                return exc, built.call_count
        return None, built.call_count

    def test_a_chrome_hung_on_the_first_cookie_is_stopped_and_a_new_one_runs(self):
        hung, fresh = self._chrome(set_cookie=[_hung("Network.setCookie")]), self._chrome()
        with self.assertLogs(_LOGGER, "WARNING") as logs:
            self.assertEqual(self._run(hung, fresh), (None, 2))
        self.assertIn("Chrome did not answer Network.setCookie", logs.output[0])
        hung.stop.assert_called_once_with()
        hung.navigate_to.assert_not_called()
        fresh.navigate_to.assert_called_once()
        fresh.stop.assert_not_called()
        self.assertIsNone(self._chrome_of_this_run)

    def test_a_chrome_hung_on_the_session_cookie_gets_one_more_run(self):
        hung = self._chrome(set_cookie=[None, _hung("Network.setCookie")])
        fresh = self._chrome()
        with self.assertLogs(_LOGGER, "WARNING"):
            self.assertEqual(self._run(hung, fresh), (None, 2))
        hung.navigate_to.assert_not_called()
        fresh.navigate_to.assert_called_once()

    def test_a_page_chrome_never_started_opening_gets_one_more_run(self):
        hung = self._chrome(navigate_to=_hung("Page.navigate"))
        fresh = self._chrome()
        with self.assertLogs(_LOGGER, "WARNING"):
            self.assertEqual(self._run(hung, fresh), (None, 2))
        fresh._wait_code_ok.assert_called_once()

    def test_it_is_only_once(self):
        first = self._chrome(set_cookie=[_hung("Network.setCookie")])
        second = self._chrome(set_cookie=[_hung("Network.setCookie")])
        with self.assertLogs(_LOGGER, "WARNING") as logs:
            raised, built = self._run(first, second)
        self.assertIsInstance(raised, TimeoutError)
        self.assertEqual((built, len(logs.output)), (2, 1))
        first.stop.assert_called_once_with()

    def test_a_failure_once_the_page_is_open_is_the_test_s_own(self):
        for failure, raises in (
            (TimeoutError("Runtime.evaluate({...})"), TimeoutError),
            (ChromeBrowserException("Script timeout exceeded"), self.failureException),
        ):
            with self.subTest(failure=failure):
                chrome = self._chrome(_wait_code_ok=failure)
                with self.assertNoLogs(_LOGGER, "WARNING"):
                    raised, built = self._run(chrome)
                self.assertIsInstance(raised, raises)
                self.assertEqual(built, 1)
                chrome.stop.assert_not_called()
