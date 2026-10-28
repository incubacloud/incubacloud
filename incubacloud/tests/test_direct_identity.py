"""On the direct path the client is the connecting host, whatever it claims.

Traefik's direct entrypoint trusts no forwarded header and strips the
CDN's; behind it, Odoo believes a forwarded chain only from the CDN's
ranges, so a header a platform host wrote names nobody.
"""
from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.net.trusted_proxies import client_ip

_CDN = ["173.245.48.0/20", "103.21.244.0/22"]


class TestTheConnectingHostIsTheClient(BaseCase):

    def test_a_forged_chain_from_a_platform_host_is_ignored(self):
        self.assertEqual(
            client_ip("37.27.0.82", "6.6.6.6, 173.245.48.1", _CDN), "37.27.0.82",
        )

    def test_from_the_proxy_gateway_it_is_the_gateway(self):
        self.assertEqual(client_ip("172.19.0.1", "6.6.6.6", _CDN), "172.19.0.1")
