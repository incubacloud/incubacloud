"""A host's address is one SSH could reach: an IP address or a host name.

The panel took any text, and a customer registered a server at
``232.123.321.22``, which reads as an IPv4 address and is not one: its
setup then failed asking to trust an SSH key no such host could have
(2026-10-09).
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.net.hostname import is_host_address


class TestHostAddress(TransactionCase):

    def _host(self, address):
        """Create a host at *address*."""
        return self.env["cloud.host"].create({
            "name": f"addr-{address}",
            "ip_address": address,
            "user": "root",
            "wildcard_domain": "addr.example.com",
        })

    def test_ip_addresses_and_host_names_are_accepted(self):
        for address in (
            "192.0.2.10", "0.0.0.0", "2001:db8::1", "server.example.com",
            "Server-1.Example.COM", "localhost",
        ):
            self.assertTrue(is_host_address(address), address)

    def test_what_no_connection_could_reach_is_refused(self):
        for address in (
            "232.123.321.22", "1.2.3", "", "  ", "bad_name.example.com",
            "-lead.example.com", "a" * 64 + ".example.com",
        ):
            self.assertFalse(is_host_address(address), address)

    def test_the_panel_refuses_a_host_at_an_impossible_address(self):
        with self.assertRaises(ValidationError):
            self._host("232.123.321.22")

    def test_a_host_bought_on_demand_starts_at_the_placeholder(self):
        self.assertEqual(self._host("0.0.0.0").ip_address, "0.0.0.0")
