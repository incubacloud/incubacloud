"""Who Odoo records as the visitor when two proxies stand in front.

Odoo reads the last entry of the forwarded chain and that is not
configurable. A reverse proxy appends the address it was reached from
before passing the request on, and it does so after its own middlewares
have run — measured on Traefik v2.11, which is why nothing configured
inside the proxy can fix this. Behind a CDN the chain therefore reads
``<visitor>, <edge>`` and every session is recorded against one of the
CDN's own addresses.

The failure is silent: the site works. So these pin both halves — that
the address is corrected when a proxy we believe asserts it, and that
nothing at all happens otherwise, because a header believed from
anywhere is one any visitor can write.
"""
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.net import real_ip

TRUSTED = "173.245.48.0/20, 2400:cb00::/32"
EDGE = "173.245.48.9"
VISITOR = "203.0.113.55"
HEADER = "CF-Connecting-IP"


def _environ(remote=EDGE, chain=None, claimed=VISITOR):
    """Return the environment a proxied request arrives with."""
    environ = {
        "REMOTE_ADDR": remote,
        "HTTP_X_FORWARDED_HOST": "www.example.test",
        "HTTP_X_FORWARDED_PROTO": "https",
        "wsgi.url_scheme": "http",
    }
    environ["HTTP_X_FORWARDED_FOR"] = (
        chain if chain is not None else f"{VISITOR}, {EDGE}"
    )
    if claimed is not None:
        environ[real_ip.environ_key(HEADER)] = claimed
    return environ


class TestReadingTheConfiguration(BaseCase):

    def test_a_header_name_becomes_its_environ_key(self):
        self.assertEqual(
            real_ip.environ_key("CF-Connecting-IP"),
            "HTTP_CF_CONNECTING_IP",
        )

    def test_networks_may_be_separated_either_way(self):
        self.assertEqual(len(real_ip.parse_networks(TRUSTED)), 2)
        self.assertEqual(
            len(real_ip.parse_networks("10.0.0.0/8 192.168.0.0/16")), 2,
        )

    def test_an_unusable_entry_is_dropped_rather_than_raising(self):
        """This runs while the server starts. A typo must not stop it,
        and dropping the entry narrows who is believed, which is the
        safe direction."""
        networks = real_ip.parse_networks("10.0.0.0/8, not-a-network")
        self.assertEqual(len(networks), 1)

    def test_nothing_configured_is_no_networks(self):
        self.assertEqual(real_ip.parse_networks(""), [])
        self.assertEqual(real_ip.parse_networks(None), [])


class TestWhoIsBelieved(BaseCase):

    def setUp(self):
        super().setUp()
        self.networks = real_ip.parse_networks(TRUSTED)

    def _client(self, **kw):
        return real_ip.asserted_client(_environ(**kw), HEADER, self.networks)

    def test_a_trusted_proxy_is_believed(self):
        self.assertEqual(self._client(), VISITOR)

    def test_a_connection_from_anywhere_else_is_not(self):
        """Otherwise any visitor could claim to be any address by
        writing the header themselves."""
        self.assertIsNone(self._client(remote="198.51.100.7"))

    def test_an_ipv6_proxy_is_believed_too(self):
        self.assertEqual(self._client(remote="2400:cb00::1"), VISITOR)

    def test_an_absent_header_changes_nothing(self):
        self.assertIsNone(self._client(claimed=None))

    def test_a_header_that_is_not_an_address_changes_nothing(self):
        self.assertIsNone(self._client(claimed="not-an-address"))

    def test_only_the_proxy_s_own_assertion_is_read(self):
        """Anything after a comma was appended further out and is not
        the proxy speaking."""
        self.assertEqual(
            self._client(claimed=f"{VISITOR}, 198.51.100.9"), VISITOR,
        )

    def test_nothing_is_believed_without_a_header_configured(self):
        self.assertIsNone(
            real_ip.asserted_client(_environ(), "", self.networks),
        )

    def test_nothing_is_believed_without_networks(self):
        self.assertIsNone(real_ip.asserted_client(_environ(), HEADER, []))


class TestRewritingTheChain(BaseCase):

    def setUp(self):
        super().setUp()
        self.networks = real_ip.parse_networks(TRUSTED)

    def test_the_visitor_becomes_the_entry_odoo_reads(self):
        environ = _environ()
        self.assertEqual(
            real_ip.rewrite(environ, HEADER, self.networks), VISITOR,
        )
        self.assertEqual(environ["HTTP_X_FORWARDED_FOR"], VISITOR)

    def test_the_edge_is_not_left_claiming_to_be_a_client(self):
        environ = _environ()
        real_ip.rewrite(environ, HEADER, self.networks)
        self.assertNotIn(EDGE, environ["HTTP_X_FORWARDED_FOR"])

    def test_an_untrusted_connection_leaves_the_chain_alone(self):
        environ = _environ(remote="198.51.100.7")
        self.assertIsNone(real_ip.rewrite(environ, HEADER, self.networks))
        self.assertEqual(
            environ["HTTP_X_FORWARDED_FOR"], f"{VISITOR}, {EDGE}",
        )


class TestInstallingItInOdoo(BaseCase):
    """The wrapper has to be the thing Odoo actually calls.

    It wraps the name ``odoo.http`` resolves on every request rather
    than the application object, because the server captures the
    application before any module is imported — replacing that would
    change nothing and nothing would say so.
    """

    def setUp(self):
        super().setUp()
        import odoo.http
        self.http = odoo.http
        self._original = odoo.http.ProxyFix
        self.addCleanup(setattr, odoo.http, "ProxyFix", self._original)

    def _config(self, header=HEADER, trusted=TRUSTED):
        return {
            real_ip.HEADER_OPTION: header,
            real_ip.TRUSTED_OPTION: trusted,
        }

    def _apply(self, environ):
        """Do exactly what ``Application.__call__`` does."""
        def app(environ, start_response):
            return []

        def start_response(status, headers):
            return

        self.http.ProxyFix(app)(environ, start_response)

    def test_odoo_records_the_visitor_once_installed(self):
        self.assertTrue(real_ip.install(self._config(), self.http))
        environ = _environ()
        self._apply(environ)
        self.assertEqual(environ["REMOTE_ADDR"], VISITOR)

    def test_without_it_odoo_records_the_edge(self):
        """The defect itself, so the test above cannot pass by accident."""
        environ = _environ()
        self._apply(environ)
        self.assertEqual(environ["REMOTE_ADDR"], EDGE)

    def test_an_installation_reached_directly_is_untouched(self):
        self.assertFalse(real_ip.install(self._config(header=""), self.http))
        self.assertFalse(real_ip.install(self._config(trusted=""), self.http))
        self.assertIs(self.http.ProxyFix, self._original)

    def test_installing_twice_wraps_once(self):
        self.assertTrue(real_ip.install(self._config(), self.http))
        wrapped = self.http.ProxyFix
        self.assertFalse(real_ip.install(self._config(), self.http))
        self.assertIs(self.http.ProxyFix, wrapped)

    def test_it_still_does_the_rest_of_the_proxy_fix(self):
        """Wrapping must not cost the scheme and host correction."""
        self.assertTrue(real_ip.install(self._config(), self.http))
        environ = _environ()
        self._apply(environ)
        self.assertEqual(environ["wsgi.url_scheme"], "https")
        self.assertEqual(environ["HTTP_HOST"], "www.example.test")


class TestWhichHeaderAHostHonours(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "real-ip-host",
            "ip_address": "198.51.100.60",
            "user": "root",
            "wildcard_domain": "realip.test",
        })

    def test_a_host_states_nothing_by_default(self):
        self.assertEqual(self.host._effective_client_ip_header(), "")

    def test_its_own_field_is_what_it_states(self):
        self.host.client_ip_header = " CF-Connecting-IP "
        self.assertEqual(
            self.host._effective_client_ip_header(), "CF-Connecting-IP",
        )


class TestWhatAnInstanceIsConfiguredWith(TransactionCase):
    """The instance is where this has to arrive: the correction runs
    inside the tenant's own Odoo, so the tenant's own configuration is
    what has to name the header and say whose word to take for it.
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )
        self.cls = DeployInstanceExecutor
        self.host = self.env["cloud.host"].create({
            "name": "conf-host",
            "ip_address": "198.51.100.61",
            "user": "root",
            "wildcard_domain": "conf.test",
        })
        project = self.env["cloud.project"].create({"name": "ConfProj"})
        self.instance = self.env["cloud.instance"].create({
            "name": "conf-inst",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _conf(self):
        """Return the rendered configuration for the fixture instance."""
        executor = self.cls.__new__(self.cls)
        executor.env = self.env
        executor._inst = lambda: self.instance
        return executor._conf_content()

    def test_the_proxy_setting_the_platform_owns_is_still_forced(self):
        self.assertIn("proxy_mode = True", self._conf())

    def test_a_host_stating_nothing_configures_nothing(self):
        conf = self._conf()
        self.assertNotIn(real_ip.HEADER_OPTION, conf)
        self.assertNotIn(real_ip.TRUSTED_OPTION, conf)

    def test_both_halves_travel_together(self):
        self.host.write({
            "client_ip_header": "CF-Connecting-IP",
            "trusted_proxy_ranges": "173.245.48.0/20\n2400:cb00::/32",
        })
        conf = self._conf()
        self.assertIn("CF-Connecting-IP", conf)
        self.assertIn("173.245.48.0/20", conf)
        self.assertIn("2400:cb00::/32", conf)

    def test_a_header_with_nobody_to_believe_is_not_written(self):
        """One believed from anywhere is one any visitor can write."""
        self.host.client_ip_header = "CF-Connecting-IP"
        self.assertNotIn(real_ip.HEADER_OPTION, self._conf())

    def test_it_is_cleared_when_the_host_stops_stating_one(self):
        """Moving an instance to a host reached directly must not leave
        it believing a header nobody sets."""
        self.host.write({
            "client_ip_header": "CF-Connecting-IP",
            "trusted_proxy_ranges": "173.245.48.0/20",
        })
        self.instance.odoo_conf = self._conf()
        self.host.client_ip_header = ""
        conf = self._conf()
        self.assertNotIn(real_ip.HEADER_OPTION, conf)
        self.assertNotIn("CF-Connecting-IP", conf)
