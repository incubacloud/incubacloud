"""The assistant's front-end tests (hoot), run as core's."""

import logging
import re

from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests.test_frontend_suite import FrontEndSuite

#: What hoot logs when the suite ends, with how many of its tests passed.
_ENDED = re.compile(r'"@incubacloud_assistant" ended \(passed: (\d+)')


class _Lines(logging.Handler):
    """Keep the messages a logger and its children write, at INFO and up."""

    def __init__(self):
        """Start with no message."""
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        """Keep *record*'s message."""
        self.lines.append(record.getMessage())


@tagged("post_install", "-at_install")
class TestTheAssistantFrontEndSuite(FrontEndSuite, HttpCase):
    """Every test under ``@incubacloud_assistant`` passes in a real browser."""

    def test_the_assistant_s_front_end_tests_pass(self):
        """The assistant's hoot suite runs, and passes.

        A suite left out of the test bundle is not an empty run: hoot finds
        no suite by that id and runs every module's tests until the browser
        times out. So the bundle must carry ours before the browser opens,
        and hoot must say afterwards that some of them passed.
        """
        ours = [
            path
            for path, *_rest in self.env["ir.asset"]._get_asset_paths(
                "web.assets_unit_tests", {}
            )
            if path.startswith("/incubacloud_assistant/static/tests/")
        ]
        self.assertTrue(
            ours, "web.assets_unit_tests carries none of the assistant's tests"
        )
        logger = logging.getLogger(__name__)
        lines = _Lines()
        level = logger.level
        logger.addHandler(lines)
        logger.setLevel(logging.INFO)
        try:
            self._run_front_end_suite("@incubacloud_assistant")
        finally:
            logger.removeHandler(lines)
            logger.setLevel(level)
        passed = [
            int(found[1]) for line in lines.lines if (found := _ENDED.search(line))
        ]
        self.assertTrue(
            passed and passed[0] > 0, "the assistant's hoot suite ran none of its tests"
        )
