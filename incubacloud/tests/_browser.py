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

    def browser_js(self, *args, **kwargs):
        """Run Odoo's ``browser_js``; once more on a Chrome that hung at the start.

        Odoo 18's ``browser_js`` sets the cookies inside the ``try`` whose
        ``finally`` stops its Chrome, so a Chrome that hung on one is
        already stopped, and the second run builds a new one. (19 sets
        them before, and its mixin stops the hung Chrome itself.)

        :return: what Odoo's ``browser_js`` returns.
        :raises TimeoutError: a timeout after the page was opened, or a
            second one before it.
        """
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
        return super().browser_js(*args, **kwargs)
