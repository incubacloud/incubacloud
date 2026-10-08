"""The new-instance form's hints match what the customer is doing.

The name's example said ``myproject-staging`` on a production instance
too, and a customer with no host ready read only "No compatible hosts
available", with no word of where to get one (8-oct-2026).
"""
import pathlib

from odoo.tests.common import TransactionCase

_FORM = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static" / "src" / "components" / "instance_detail" / "instance_detail.xml"
)


class TestNewInstanceFormHints(TransactionCase):

    def test_the_name_example_follows_the_environment(self):
        template = _FORM.read_text()
        self.assertNotIn('placeholder="myproject-staging"', template)
        self.assertIn(
            "state.form.environment === 'staging' ? 'myproject-staging' : 'myproject'",
            template,
        )

    def test_no_host_offers_a_button_to_the_hosts_page(self):
        """A button rather than a sentence: the first customer asked how
        to pick a host with the sentence in front of them (9-oct-2026)."""
        template = _FORM.read_text()
        self.assertNotIn("No compatible hosts available.", template)
        self.assertIn('class="rl-btn rl-btn-sm ic-no-host-add"', template)
        self.assertIn("env.navigate('hosts', {})", template)
