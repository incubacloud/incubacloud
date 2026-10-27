"""The MCP's front-end tests (hoot), run as core's."""

from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests.test_frontend_suite import FrontEndSuite


@tagged("post_install", "-at_install")
class TestTheMcpFrontEndSuite(FrontEndSuite, HttpCase):
    """Every test under ``@incubacloud_mcp`` passes in a real browser."""

    def test_the_connect_page_s_front_end_tests_pass(self):
        """The hoot suite of the Connect an agent page passes."""
        self._run_front_end_suite("@incubacloud_mcp")
