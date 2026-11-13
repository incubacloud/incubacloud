"""What the tests that drive a browser share: one more Chrome when it hangs.

On GitHub's runners Chrome now and then stops answering at the very start
of a test: it takes the first commands, then never acknowledges the
session cookie, and Odoo gives up after ten seconds (three red runs on
2-nov-2026, each test green on the next run and in devel). Nothing of the
test has run by then, so starting a new Chrome and running it again is
safe. Odoo's own retry (``ODOO_TEST_FAILURE_RETRIES``) is not: it also
repeats every test that logs an error on purpose.
"""
import logging

_logger = logging.getLogger(__name__)

#: What Odoo sends Chrome before the test's code runs: the cookies, and
#: the order to open the page. A timeout on any of them means the test has
#: not started (``Page.navigate`` hung the same way on 2-nov-2026).
_BEFORE_THE_PAGE = ("Network.setCookie(", "Network.deleteCookies(", "Page.navigate(")


class ChromeStartRetry:
    """Mixin for an ``HttpCase``: run ``browser_js`` again, once, when
    Chrome did not answer before the page was opened."""

    #: The Chrome the running ``browser_js`` drives, as Odoo hands it to
    #: ``allow_requests``; None outside ``browser_js``.
    _chrome_of_this_run = None

    def allow_requests(self, browser=None, all_requests=False):
        """Odoo's ``allow_requests``, noting the Chrome it is given.

        ``browser_js`` hands its Chrome here before it takes charge of
        closing it: when Chrome hangs on this very cookie, nothing closes
        it, and the note is what ``browser_js`` stops it by.

        :param browser: the ``ChromeBrowser`` the cookie is set on, if any.
        :param all_requests: passed on to Odoo's.
        :return: Odoo's context manager.
        """
        if browser is not None:
            self._chrome_of_this_run = browser
        return super().allow_requests(browser=browser, all_requests=all_requests)

    def browser_js(self, *args, **kwargs):
        """Run Odoo's ``browser_js``; once more on a Chrome that hung at the start.

        The hung Chrome is stopped before the second run, which builds a
        new one. When Odoo had already closed it, stopping it does nothing.

        :return: what Odoo's ``browser_js`` returns.
        :raises TimeoutError: a timeout after the page was opened, or a
            second one before it.
        """
        try:
            try:
                return super().browser_js(*args, **kwargs)
            except TimeoutError as exc:
                if not str(exc).startswith(_BEFORE_THE_PAGE):
                    raise
                _logger.warning(
                    "Chrome did not answer %s before the page was opened;"
                    " running the test once more on a new one",
                    str(exc).split("(", 1)[0],
                )
            if self._chrome_of_this_run is not None:
                self._chrome_of_this_run.stop()
            return super().browser_js(*args, **kwargs)
        finally:
            self._chrome_of_this_run = None
