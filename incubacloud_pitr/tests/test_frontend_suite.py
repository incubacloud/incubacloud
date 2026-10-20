"""The continuous recovery card's front-end tests (hoot), run as core's."""
from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests.test_frontend_suite import FrontEndSuite


@tagged("post_install", "-at_install")
class TestThePitrFrontEndSuite(FrontEndSuite, HttpCase):
    """Every test under ``@incubacloud_pitr`` passes in a real browser."""

    def test_the_card_s_front_end_tests_pass(self):
        self._run_front_end_suite("@incubacloud_pitr")
