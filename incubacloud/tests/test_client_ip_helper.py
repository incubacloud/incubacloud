"""The controller helper, against a request shaped like a real one.

Odoo's own ``--proxy-mode`` resolves the caller with
``ProxyFix(x_for=1)``, which reads the last entry of the forwarded
chain. That is right with one proxy in front and wrong with two, and a
host behind a CDN always has two. Confirmed against the shipped
werkzeug: a chain of ``<client>, <edge>`` resolves to the edge. These
cover the helper that reads the chain against the ranges the
installation actually declared.

``access_route`` and ``remote_addr`` come from ``werkzeug.wrappers``,
which is what the mocks are specified against — Odoo's own wrapper
forwards to it through a name whitelist rather than defining them.
"""
from unittest.mock import MagicMock, patch

import werkzeug.wrappers
from odoo.http import Request

from odoo.tests.common import TransactionCase

from ..controllers import _client_ip

EDGE = ["198.51.100.0/24"]


class TestClientIpHelper(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.trusted_proxy_ranges = ""

    def _resolve(self, remote_addr, access_route):
        """Run the helper against a request with the given addresses."""
        httprequest = MagicMock(spec=werkzeug.wrappers.Request)
        httprequest.remote_addr = remote_addr
        httprequest.access_route = access_route
        fake = MagicMock(spec=Request)
        fake.httprequest = httprequest
        fake.env = self.env
        with patch.object(_client_ip, "request", fake):
            return _client_ip.client_ip()

    def test_with_nothing_declared_the_connection_is_the_answer(self):
        self.assertEqual(
            self._resolve("198.51.100.7", ["203.0.113.9", "198.51.100.7"]),
            "198.51.100.7",
        )

    def test_behind_a_declared_proxy_the_visitor_is_the_answer(self):
        self.settings.trusted_proxy_ranges = "\n".join(EDGE)
        self.assertEqual(
            self._resolve("198.51.100.7", ["203.0.113.9", "198.51.100.7"]),
            "203.0.113.9",
        )

    def test_a_direct_caller_cannot_forge_an_identity(self):
        self.settings.trusted_proxy_ranges = "\n".join(EDGE)
        self.assertEqual(
            self._resolve("203.0.113.9", ["1.1.1.1"]), "203.0.113.9",
        )

    def test_an_address_less_request_is_named_rather_than_crashing(self):
        # This runs on the refusal path of public endpoints, where an
        # exception would hand the caller a better outcome than a 429.
        self.assertEqual(self._resolve(None, []), "unknown")

    def test_no_request_at_all_is_named_too(self):
        with patch.object(_client_ip, "request", None):
            self.assertEqual(_client_ip.client_ip(), "unknown")
