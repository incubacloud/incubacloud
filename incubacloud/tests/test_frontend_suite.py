"""The front end's own tests (hoot), run where the Python suite runs.

``static/tests/*.test.js`` sit in ``web.assets_unit_tests`` and only ran
when someone opened ``/web/tests`` in a browser: no job did, so about
eight hundred tests of the OWL panel never ran anywhere until
20-oct-2026. This drives Odoo's own runner, headless, over one module's
tests only, the way ``web``'s ``WebSuite`` drives the whole bundle.
"""
import unittest

from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tests.common import _find_executable

from odoo.addons.web.tests.test_js import unit_test_error_checker


def _suite_id(name):
    """The id hoot gives the suite *name*, as ``web``'s runner computes it.

    A text filter would also match other modules' tests that mention the
    name; the id selects this module's suite and nothing else.

    :param str name: the suite, e.g. ``@incubacloud``
    :rtype: str
    """
    value = 0
    for char in name:
        value = ((value << 5) - value + ord(char)) & 0xFFFFFFFF
    return f"{value:08x}"


class FrontEndSuite:
    """Run one module's hoot suite in a headless browser.

    A mixin, not a test case, so a module that runs its own suite with
    it (the tenant panel's) does not collect this module's test as well.
    Mixed into an ``HttpCase``.
    """

    def _run_front_end_suite(self, suite):
        """Run every hoot test under *suite*; fail on any failure.

        Odoo skips a browser test when it finds no Chrome, and a suite
        skipped that way is the one this exists to stop: it fails. It
        signs in as a user of its own: on a copy of production (the
        deploy's check of the image) ``admin`` does not answer to the
        test password. A system administrator: a tenant panel keeps
        ``/web``, the runner's page included, from everyone else
        (``incubacloud_tenant/models/ir_http.py``), and it is installed
        with core in one of saas's CI legs.

        :param str suite: the suite, e.g. ``@incubacloud``
        """
        try:
            _find_executable()
        except unittest.SkipTest:
            self.fail("no Chrome or Chromium here: the front end's tests need one")
        new_test_user(
            self.env, login="hoot-runner", password="hoot-runner",
            groups="base.group_user,base.group_system",
        )
        self.browser_js(
            "/web/tests?headless&loglevel=2&preset=desktop&timeout=15000"
            f"&id={_suite_id(suite)}",
            "",
            "",
            login="hoot-runner",
            # The suite takes seconds: a page that never runs it fails fast.
            timeout=600,
            success_signal="[HOOT] Test suite succeeded",
            error_checker=unit_test_error_checker,
        )


@tagged("post_install", "-at_install")
class TestTheFrontEndSuite(FrontEndSuite, HttpCase):
    """Every test under ``@incubacloud`` passes in a real browser."""

    def test_the_panel_s_front_end_tests_pass(self):
        self._run_front_end_suite("@incubacloud")
