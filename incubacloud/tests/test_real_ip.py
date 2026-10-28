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

The requests are built as they reach Odoo, not as they leave the CDN:
the connection comes from our own proxy's container, and the CDN's edge
is the last entry of the chain, which that proxy appended. Until
2026-10-27 they were built with the edge as the connection, a request
that never arrives — and every one of them passed while production
recorded nothing but edges.

And Odoo does not share its network with our proxy alone. On the master
and on every pool host each panel's Odoo sits on the same Docker network
as Traefik and as every other panel, so "a private address" names the
neighbours too. Where the proxy is named, only the proxy may speak for
somebody else.
"""
import configparser
import pathlib
import socket
from unittest.mock import patch

from werkzeug.wrappers import Request

from odoo.modules.module import get_manifest, get_module_path
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.net import real_ip, trusted_proxies

TRUSTED = "173.245.48.0/20, 2400:cb00::/32"
EDGE = "173.245.48.9"
VISITOR = "203.0.113.55"
HEADER = "CF-Connecting-IP"
#: Our own proxy, as Odoo sees it: Traefik's address on the Docker
#: network both containers share.
PROXY = "172.18.0.5"
#: The container Full Setup runs that proxy as, on every host and on
#: the master, as the platform writes it: with the final dot that makes
#: it an absolute name, which no search domain is appended to.
PROXY_NAME = "inverseproxy-proxy-1."
#: Another container on that same network: a neighbouring panel.
NEIGHBOUR = "172.18.0.9"
#: Somebody on the internet who is neither the CDN nor a visitor of it.
OUTSIDER = "198.51.100.7"
#: The address a caller would like to be recorded as.
FORGED = "192.0.2.66"
#: A tenant panel's server, calling the master.
TENANT_HOST = "37.27.0.82"
#: The option naming our proxy, spelled out so a test that predates it
#: fails on what it asserts rather than on a missing name.
PROXY_OPTION = "x_real_ip_proxy"
#: Where the module says what it could not do.
LOGGER = "odoo.addons.incubacloud.net.real_ip"


def _gaierror(code):
    """Return the resolver's error *code* (``'EAI_AGAIN'``...) as raised."""
    return socket.gaierror(getattr(socket, code), code)


def _environ(remote=PROXY, chain=None, claimed=VISITOR):
    """Return the environment a proxied request arrives with.

    By default, a visitor reaching Odoo through the CDN and our proxy:
    the proxy connects to Odoo from its container, and the chain ends
    with the edge the proxy was reached from.
    """
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


def _answer(*addresses):
    """Return what ``getaddrinfo`` answers for a name at *addresses*."""
    answers = []
    for address in addresses:
        if ":" in address:
            answers.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                            (address, 0, 0, 0)))
        else:
            answers.append((socket.AF_INET, socket.SOCK_STREAM, 6, "",
                            (address, 0)))
    return answers


class _DockerAnswers:
    """Have the name of our proxy resolve the way Docker would.

    The resolver and the clock are replaced on the module only, with
    their real signatures, so a call that would not work against the
    real ones fails here too.
    """

    def _docker_answers(self, *addresses):
        """Make the proxy's name resolve to *addresses* from now on.

        :return: the patched resolver, to assert on its calls
        """
        patcher = patch.object(
            real_ip, "getaddrinfo", autospec=True,
            return_value=_answer(*addresses),
        )
        resolver = patcher.start()
        self.addCleanup(patcher.stop)
        return resolver

    def _frozen_clock(self, at=0.0):
        """Stop the module's clock at *at*; move it with ``return_value``."""
        patcher = patch.object(
            real_ip, "monotonic", autospec=True, return_value=at,
        )
        clock = patcher.start()
        self.addCleanup(patcher.stop)
        return clock


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

    def test_the_proxy_option_is_the_one_deploys_write(self):
        self.assertEqual(real_ip.PROXY_OPTION, PROXY_OPTION)

    def test_only_global_networks_can_be_the_cdn_s(self):
        """A private range in the CDN's list would let anything on Odoo's
        network speak as the CDN. Dropped, and said so."""
        with self.assertLogs(LOGGER, "WARNING") as logged:
            networks = real_ip.cdn_networks(
                "173.245.48.0/20, 172.16.0.0/12, 10.0.0.0/8, 2400:cb00::/32,"
                " fd00::/8, 100.64.0.0/10",
            )
        self.assertEqual(
            [str(network) for network in networks],
            ["173.245.48.0/20", "2400:cb00::/32"],
        )
        self.assertEqual(len(logged.records), 4)


class TestWhoIsOurProxy(_DockerAnswers, BaseCase):
    """Which connections to Odoo count as our own proxy's."""

    def test_unnamed_it_is_anything_on_this_machine_or_a_private_net(self):
        """The fallback, and its limit: a neighbour on a shared network
        is a private address too."""
        proxy = real_ip.OwnProxy("")
        for address in ("127.0.0.1", "::1", PROXY, NEIGHBOUR, "10.0.3.4",
                        "192.168.1.20", "fd12:3456::5"):
            with self.subTest(address=address):
                self.assertTrue(proxy.is_ours(address))
        self.assertFalse(proxy.is_ours(OUTSIDER))
        self.assertFalse(proxy.is_ours(EDGE))

    def test_an_address_names_itself_and_nobody_else(self):
        proxy = real_ip.OwnProxy(PROXY)
        self.assertTrue(proxy.is_ours(PROXY))
        self.assertFalse(proxy.is_ours(NEIGHBOUR))
        self.assertFalse(proxy.is_ours("127.0.0.1"))

    def test_a_name_is_what_docker_answers_for_it(self):
        resolver = self._docker_answers(PROXY)
        proxy = real_ip.OwnProxy(PROXY_NAME)
        self.assertTrue(proxy.is_ours(PROXY))
        self.assertFalse(proxy.is_ours(NEIGHBOUR))
        self.assertEqual(resolver.call_args.args[0], PROXY_NAME)

    def test_a_name_that_does_not_exist_falls_back_to_unnamed(self):
        """Not to nobody: that would record every visitor as the proxy,
        one address for every limit keyed on it. Unnamed is the weaker
        guarantee the installation had before naming it, and it is said
        loudly."""
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_NONAME")
        proxy = real_ip.OwnProxy(PROXY_NAME)
        with self.assertLogs(LOGGER, "ERROR"):
            self.assertTrue(proxy.is_ours(PROXY))
        self.assertTrue(proxy.is_ours(NEIGHBOUR))
        self.assertFalse(proxy.is_ours(OUTSIDER))

    def test_a_name_never_resolved_falls_back_whatever_the_error(self):
        """What a missing container gets from Docker's resolver: a
        temporary failure, not a missing name (measured, 27-oct-2026)."""
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_AGAIN")
        proxy = real_ip.OwnProxy(PROXY_NAME)
        with self.assertLogs(LOGGER, "ERROR"):
            self.assertTrue(proxy.is_ours(PROXY))

    def test_a_temporary_failure_keeps_the_last_good_answer(self):
        resolver = self._docker_answers(PROXY)
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        self.assertTrue(proxy.is_ours(PROXY))
        resolver.side_effect = _gaierror("EAI_AGAIN")
        clock.return_value = real_ip.PROXY_TTL_SECONDS
        with self.assertNoLogs(LOGGER, "ERROR"):
            self.assertTrue(proxy.is_ours(PROXY))
            self.assertFalse(proxy.is_ours(NEIGHBOUR))
        self.assertEqual(resolver.call_count, 2)

    def test_a_failure_that_outlasts_the_last_answer_falls_back(self):
        """A container renamed for good answers like a hiccup that never
        ends: the old address is kept only for so long."""
        resolver = self._docker_answers(PROXY)
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        proxy.is_ours(PROXY)
        resolver.side_effect = _gaierror("EAI_AGAIN")
        clock.return_value = real_ip.PROXY_TTL_SECONDS
        proxy.is_ours(PROXY)
        clock.return_value = (
            real_ip.PROXY_TTL_SECONDS + real_ip.PROXY_STALE_SECONDS
        )
        with self.assertLogs(LOGGER, "ERROR"):
            self.assertTrue(proxy.is_ours(NEIGHBOUR))

    def test_a_name_that_disappears_keeps_its_last_answer_a_while(self):
        """Whatever the resolver says: which error a missing container
        gets depends on Docker's version, not on whether it is gone."""
        resolver = self._docker_answers(PROXY)
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        proxy.is_ours(PROXY)
        resolver.side_effect = _gaierror("EAI_NONAME")
        clock.return_value = real_ip.PROXY_TTL_SECONDS
        with self.assertNoLogs(LOGGER, "ERROR"):
            self.assertTrue(proxy.is_ours(PROXY))
            self.assertFalse(proxy.is_ours(NEIGHBOUR))
        clock.return_value = (
            real_ip.PROXY_TTL_SECONDS + real_ip.PROXY_STALE_SECONDS
        )
        with self.assertLogs(LOGGER, "ERROR"):
            self.assertTrue(proxy.is_ours(NEIGHBOUR))

    def test_a_name_that_never_resolved_is_asked_again_in_seconds(self):
        """Falling back is not giving up: the proxy may simply have
        started after Odoo."""
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_AGAIN")
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        with self.assertLogs(LOGGER, "ERROR"):
            proxy.is_ours(PROXY)
        resolver.side_effect = None
        resolver.return_value = _answer(PROXY)
        clock.return_value = real_ip.PROXY_RETRY_SECONDS
        self.assertFalse(proxy.is_ours(NEIGHBOUR))
        self.assertEqual(resolver.call_count, 2)

    def test_fallen_back_a_neighbour_still_cannot_force_lookups(self):
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_AGAIN")
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        with self.assertLogs(LOGGER, "ERROR"):
            for second in range(int(real_ip.PROXY_RETRY_SECONDS)):
                clock.return_value = float(second)
                proxy.is_ours(NEIGHBOUR)
        self.assertEqual(resolver.call_count, 1)

    def test_the_error_is_said_once_in_a_while_not_per_request(self):
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_NONAME")
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        with self.assertLogs(LOGGER, "ERROR") as logged:
            for step in range(5):
                clock.return_value = step * real_ip.PROXY_TTL_SECONDS
                proxy.is_ours(PROXY)
        self.assertEqual(resolver.call_count, 5)
        self.assertEqual(len(logged.records), 1)

    def test_a_name_that_resolves_again_is_named_again(self):
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_NONAME")
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        with self.assertLogs(LOGGER, "ERROR"):
            proxy.is_ours(PROXY)
        resolver.side_effect = None
        resolver.return_value = _answer(PROXY)
        clock.return_value = real_ip.PROXY_TTL_SECONDS
        self.assertTrue(proxy.is_ours(PROXY))
        self.assertFalse(proxy.is_ours(NEIGHBOUR))

    def test_an_answer_is_reused_for_a_while(self):
        resolver = self._docker_answers(PROXY)
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        proxy.is_ours(PROXY)
        clock.return_value = real_ip.PROXY_TTL_SECONDS - 1
        proxy.is_ours(PROXY)
        self.assertEqual(resolver.call_count, 1)

    def test_an_answer_is_asked_again_once_old(self):
        resolver = self._docker_answers(PROXY)
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        proxy.is_ours(PROXY)
        clock.return_value = real_ip.PROXY_TTL_SECONDS
        proxy.is_ours(PROXY)
        self.assertEqual(resolver.call_count, 2)

    def test_a_proxy_back_on_a_new_address_is_recognised_in_seconds(self):
        """A restart or a recreated container: the stale answer must not
        turn every visitor into the proxy's new address for long."""
        resolver = self._docker_answers(PROXY)
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        self.assertTrue(proxy.is_ours(PROXY))
        resolver.return_value = _answer("172.18.0.7")
        clock.return_value = real_ip.PROXY_RETRY_SECONDS
        self.assertTrue(proxy.is_ours("172.18.0.7"))

    def test_a_neighbour_cannot_make_every_request_a_lookup(self):
        resolver = self._docker_answers(PROXY)
        clock = self._frozen_clock()
        proxy = real_ip.OwnProxy(PROXY_NAME)
        for second in range(int(real_ip.PROXY_RETRY_SECONDS)):
            clock.return_value = float(second)
            self.assertFalse(proxy.is_ours(NEIGHBOUR))
        self.assertEqual(resolver.call_count, 1)

    def test_a_dual_stack_socket_still_names_the_proxy(self):
        """Odoo listening on ``::`` reports an IPv4 peer as
        ``::ffff:a.b.c.d``, which no IPv4 address equals."""
        self.assertTrue(real_ip.OwnProxy(PROXY).is_ours(f"::ffff:{PROXY}"))
        self.assertTrue(real_ip.OwnProxy("").is_ours(f"::ffff:{PROXY}"))


class TestWhoIsBelieved(BaseCase):

    def setUp(self):
        super().setUp()
        self.networks = real_ip.parse_networks(TRUSTED)

    def _client(self, **kw):
        return real_ip.asserted_client(_environ(**kw), HEADER, self.networks)

    def _named(self, **kw):
        """Ask with our proxy named by its address."""
        return real_ip.asserted_client(
            _environ(**kw), HEADER, self.networks, real_ip.OwnProxy(PROXY),
        )

    def test_the_request_production_receives_is_believed(self):
        """The case that was never corrected: our proxy connects from
        its Docker address, the edge is the hop it appended, and the
        CDN names the visitor."""
        environ = {
            "REMOTE_ADDR": "172.18.0.5",
            "HTTP_X_FORWARDED_HOST": "www.incubacloud.io",
            "HTTP_X_FORWARDED_FOR": "203.0.113.55, 173.245.48.9",
            "HTTP_CF_CONNECTING_IP": "203.0.113.55",
        }
        self.assertEqual(
            real_ip.asserted_client(environ, HEADER, self.networks),
            "203.0.113.55",
        )

    def test_the_named_proxy_is_believed(self):
        self.assertEqual(self._named(), VISITOR)

    def test_a_neighbour_of_the_named_proxy_is_not(self):
        """Another panel on the same network, with the chain and the
        header our proxy would have sent."""
        self.assertIsNone(
            self._named(remote=NEIGHBOUR, chain=f"{FORGED}, {EDGE}",
                        claimed=FORGED),
        )

    def test_a_proxy_on_the_same_machine_is_ours(self):
        self.assertEqual(self._client(remote="127.0.0.1"), VISITOR)
        self.assertEqual(self._client(remote="::1"), VISITOR)

    def test_a_proxy_on_any_private_network_is_ours(self):
        for remote in ("10.0.3.4", "192.168.1.20", "fd12:3456::5"):
            with self.subTest(remote=remote):
                self.assertEqual(self._client(remote=remote), VISITOR)

    def test_a_proxy_seen_through_a_dual_stack_socket_is_ours(self):
        """Odoo listening on ``::`` reports an IPv4 peer as
        ``::ffff:a.b.c.d``, which no IPv4 network contains."""
        self.assertEqual(self._client(remote="::ffff:172.18.0.5"), VISITOR)

    def test_an_ipv6_edge_is_believed_too(self):
        self.assertEqual(
            self._client(chain=f"{VISITOR}, 2400:cb00::1"), VISITOR,
        )

    def test_a_proxy_that_does_not_trust_the_edge_still_names_it(self):
        """Without ``trustedIPs`` our proxy drops the CDN's chain and
        keeps only the edge it was reached from. That edge is still the
        one entry nobody but our proxy writes."""
        self.assertEqual(self._client(chain=EDGE), VISITOR)

    def test_the_cdn_s_word_wins_over_a_chain_a_visitor_wrote(self):
        """A visitor's own ``X-Forwarded-For`` travels through the CDN
        to the left of the address the CDN appends. None of it is read."""
        self.assertEqual(
            self._client(chain=f"{FORGED}, {VISITOR}, {EDGE}"), VISITOR,
        )

    def test_a_request_that_skipped_the_cdn_is_not_believed(self):
        """Somebody reaching our proxy at the origin's address. The
        proxy discards the chain they wrote and appends their real
        address, so the last hop is theirs whatever they claim."""
        self.assertIsNone(self._client(chain=OUTSIDER, claimed=FORGED))

    def test_an_edge_written_further_left_is_not_believed(self):
        """A proxy that kept the caller's chain still appends the
        caller last; an edge anywhere before that was typed."""
        self.assertIsNone(
            self._client(chain=f"{EDGE}, {OUTSIDER}", claimed=FORGED),
        )

    def test_a_connection_from_outside_is_not_ours(self):
        """Odoo exposed on a public address: the caller wrote the whole
        chain, edge included, so neither it nor the header is read."""
        self.assertIsNone(
            self._client(remote=OUTSIDER, chain=f"{FORGED}, {EDGE}",
                         claimed=FORGED),
        )

    def test_the_cdn_connecting_to_odoo_itself_is_believed(self):
        """A CDN pointed straight at Odoo, no proxy of ours in between:
        the edge is the connection, and only the CDN connects from it."""
        self.assertEqual(self._client(remote=EDGE, chain=VISITOR), VISITOR)
        self.assertEqual(self._named(remote=EDGE, chain=VISITOR), VISITOR)

    def test_a_request_without_a_chain_is_not_believed(self):
        """Our proxy calling Odoo itself, such as a health check."""
        self.assertIsNone(self._client(chain=""))

    def test_a_last_hop_that_is_not_an_address_is_not_believed(self):
        self.assertIsNone(self._client(chain=f"{VISITOR}, unknown"))

    def test_an_absent_header_changes_nothing(self):
        self.assertIsNone(self._client(claimed=None))

    def test_a_header_that_is_not_an_address_changes_nothing(self):
        self.assertIsNone(self._client(claimed="not-an-address"))

    def test_a_header_with_a_zone_changes_nothing(self):
        """``fe80::1%eth0`` parses, and the zone is whatever the sender
        typed: not something to store as somebody's address."""
        self.assertIsNone(self._client(claimed="fe80::1%eth0"))

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

    def test_a_private_range_in_the_list_makes_no_neighbour_the_cdn(self):
        """Even past the filter at start-up — a list handed in directly,
        or one as wide as everything: the CDN is somebody on the
        internet, and an address that is not global never speaks as it,
        whether it connects or is the hop our proxy appended."""
        for raw in ("172.16.0.0/12, " + TRUSTED, "0.0.0.0/0, ::/0"):
            networks = real_ip.parse_networks(raw)
            for kw in (
                {"remote": NEIGHBOUR, "chain": FORGED},
                {"chain": f"{FORGED}, {NEIGHBOUR}"},
            ):
                with self.subTest(raw=raw, **kw):
                    self.assertIsNone(real_ip.asserted_client(
                        _environ(claimed=FORGED, **kw), HEADER, networks,
                        real_ip.OwnProxy(PROXY),
                    ))

    def test_the_visitor_is_written_as_an_address_is(self):
        """One spelling per address, whatever the header used."""
        self.assertEqual(
            self._client(claimed="::ffff:203.0.113.55"), VISITOR,
        )
        self.assertEqual(self._client(claimed="2001:DB8::0:1"), "2001:db8::1")


class TestRewritingTheChain(BaseCase):

    def setUp(self):
        super().setUp()
        self.networks = real_ip.parse_networks(TRUSTED)

    def _rewrite(self, environ, proxy=""):
        """Rewrite *environ* with our proxy named by *proxy*."""
        return real_ip.rewrite(
            environ, HEADER, self.networks, real_ip.OwnProxy(proxy),
        )

    def test_the_visitor_becomes_the_entry_odoo_reads(self):
        environ = _environ()
        self.assertEqual(self._rewrite(environ), VISITOR)
        self.assertEqual(environ["HTTP_X_FORWARDED_FOR"], VISITOR)

    def test_the_edge_is_not_left_claiming_to_be_a_client(self):
        environ = _environ()
        self._rewrite(environ)
        self.assertNotIn(EDGE, environ["HTTP_X_FORWARDED_FOR"])

    def test_an_outsider_s_chain_is_its_own_address(self):
        """Whatever chain a caller that is not our proxy sends, it wrote:
        the only entry Odoo is left to read is the caller itself."""
        environ = _environ(remote=OUTSIDER)
        self.assertIsNone(self._rewrite(environ))
        self.assertEqual(environ["HTTP_X_FORWARDED_FOR"], OUTSIDER)

    def test_a_neighbour_s_chain_is_its_own_address(self):
        environ = _environ(remote=NEIGHBOUR, chain=FORGED, claimed=None)
        self.assertIsNone(self._rewrite(environ, proxy=PROXY))
        self.assertEqual(environ["HTTP_X_FORWARDED_FOR"], NEIGHBOUR)

    def test_a_request_that_skipped_the_cdn_keeps_its_chain(self):
        """Our proxy wrote it: the last entry is the real caller."""
        environ = _environ(chain=OUTSIDER, claimed=FORGED)
        self.assertIsNone(self._rewrite(environ, proxy=PROXY))
        self.assertEqual(environ["HTTP_X_FORWARDED_FOR"], OUTSIDER)

    def test_a_request_without_a_chain_is_given_none(self):
        """A health check from inside the container sends none; there
        is nothing to neutralise and nothing is added."""
        environ = _environ(remote="127.0.0.1", claimed=None)
        del environ["HTTP_X_FORWARDED_FOR"]
        self.assertIsNone(self._rewrite(environ, proxy=PROXY))
        self.assertNotIn("HTTP_X_FORWARDED_FOR", environ)


class _OnTheUncorrectedProxyFix:
    """Run each test against Odoo's own ``ProxyFix``, restored after.

    On a panel that *is* behind a CDN the correction is already
    installed by the time any test runs — it happens at import — so
    asserting on the uncorrected behaviour, or on installing, would be
    asserting against a state the test never created. That is how the
    installation tests first passed everywhere and failed on the one
    installation that had it configured.
    """

    def setUp(self):
        """Swap in Odoo's own ``ProxyFix`` and restore whatever ran."""
        super().setUp()
        import odoo.http
        self.http = odoo.http
        current = odoo.http.ProxyFix
        self.addCleanup(setattr, odoo.http, "ProxyFix", current)
        self._original = real_ip.uninstalled(current)
        odoo.http.ProxyFix = self._original

    def _config(self, header=HEADER, trusted=TRUSTED, proxy=""):
        """Return the options as Odoo's configuration would carry them."""
        return {
            real_ip.HEADER_OPTION: header,
            real_ip.TRUSTED_OPTION: trusted,
            PROXY_OPTION: proxy,
        }

    def _apply(self, environ):
        """Do exactly what ``Application.__call__`` does.

        :return: the request Odoo then builds from *environ*
        :rtype: werkzeug.wrappers.Request
        """
        def app(environ, start_response):
            return []

        def start_response(status, headers):
            return

        self.http.ProxyFix(app)(environ, start_response)
        return Request(environ)


class TestInstallingItInOdoo(
    _DockerAnswers, _OnTheUncorrectedProxyFix, BaseCase,
):
    """The wrapper has to be the thing Odoo actually calls.

    It wraps the name ``odoo.http`` resolves on every request rather
    than the application object, because the server captures the
    application before any module is imported — replacing that would
    change nothing and nothing would say so.
    """

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

    def test_the_wrapper_says_what_it_replaced(self):
        """The only way back to the original: installing happens once,
        at import, and nothing else keeps a reference."""
        self.assertTrue(real_ip.install(self._config(), self.http))
        self.assertIs(
            real_ip.uninstalled(self.http.ProxyFix), self._original,
        )

    def test_uninstalled_is_the_identity_for_anything_else(self):
        self.assertIs(
            real_ip.uninstalled(self._original), self._original,
        )

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

    def test_installing_asks_for_the_name_once_and_warns_if_it_fails(self):
        """At start-up, so a name that does not resolve is in the log
        before the first visitor is recorded under the fallback. A
        warning, not an error: the job runner loads the same
        configuration away from Traefik's network, on every deploy, and
        never serves a proxied request."""
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_AGAIN")
        with self.assertLogs(LOGGER, "WARNING") as logged:
            self.assertTrue(
                real_ip.install(self._config(proxy=PROXY_NAME), self.http),
            )
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(
            [record.levelname for record in logged.records], ["WARNING"],
        )
        self.assertEqual(self._apply(_environ()).remote_addr, VISITOR)

    def test_proxied_traffic_while_the_name_does_not_resolve_is_an_error(self):
        """What the warning could not tell: a server recording visitors
        under the fallback. The warning does not use up the error."""
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_AGAIN")
        clock = self._frozen_clock()
        with self.assertLogs(LOGGER, "WARNING"):
            real_ip.install(self._config(proxy=PROXY_NAME), self.http)
        clock.return_value = real_ip.PROXY_RETRY_SECONDS
        with self.assertLogs(LOGGER, "ERROR"):
            request = self._apply(_environ())
        self.assertEqual(request.remote_addr, VISITOR)

    def test_a_private_range_in_the_configuration_is_ignored(self):
        """A host's own ``trusted_proxy_ranges`` wins over the CDN's list
        and may name a private network. Through our proxy, a neighbour
        that reached it over that network would be the last hop; straight
        to Odoo, it would be the CDN connecting. Neither speaks as it."""
        trusted = "172.16.0.0/12, " + TRUSTED
        with self.assertLogs(LOGGER, "WARNING"):
            self.assertTrue(real_ip.install(
                self._config(trusted=trusted, proxy=PROXY), self.http,
            ))
        through_the_proxy = self._apply(_environ(
            chain=f"{FORGED}, {NEIGHBOUR}", claimed=FORGED,
        ))
        self.assertEqual(through_the_proxy.remote_addr, NEIGHBOUR)
        straight_in = self._apply(_environ(
            remote=NEIGHBOUR, chain=FORGED, claimed=FORGED,
        ))
        self.assertEqual(straight_in.remote_addr, NEIGHBOUR)

    def test_a_range_that_covers_private_addresses_is_pointed_out(self):
        """Global as a whole, ``128.0.0.0/1`` still spans 172.16.0.0/12
        and 192.168.0.0/16; nothing private is believed from it, but the
        list is almost certainly not what was meant.

        Not ``0.0.0.0/0``: whether that one is global changed with
        CPython's fix for CVE-2024-4032, which the Python 3.10 of the CI
        image lacks; ``128.0.0.0/1`` is global on both sides of it."""
        with self.assertLogs(LOGGER, "WARNING") as logged:
            self.assertTrue(real_ip.install(
                self._config(trusted="128.0.0.0/1", proxy=PROXY), self.http,
            ))
        self.assertIn("overlap", logged.output[0])
        request = self._apply(_environ(remote=NEIGHBOUR, chain=FORGED,
                                       claimed=FORGED))
        self.assertEqual(request.remote_addr, NEIGHBOUR)

    def test_a_proxy_named_for_real_is_looked_up_for_real(self):
        """No stand-in for the resolver: ``localhost`` is in every
        container's hosts file."""
        config = self._config(proxy="localhost")
        self.assertTrue(real_ip.install(config, self.http))
        self.assertEqual(
            self._apply(_environ(remote="127.0.0.1")).remote_addr, VISITOR,
        )
        self.assertEqual(
            self._apply(_environ(remote=PROXY, chain=FORGED)).remote_addr,
            PROXY,
        )


class TestEachMode(_DockerAnswers, _OnTheUncorrectedProxyFix, BaseCase):
    """What Odoo records, in each way an installation is reached.

    ``client_ip()`` is checked alongside where a mode uses it: it walks
    the chain against the ranges the installation's Settings declare,
    and on the master those are the CDN's, so it found the visitor even
    while Odoo itself recorded the edge. It must keep finding them.
    """

    RANGES = [entry.strip() for entry in TRUSTED.split(",")]

    def _record(self, installed=True, proxy="", **kw):
        """Return the request Odoo builds, with or without the correction.

        :param bool installed: install the correction first
        :param str proxy: the value of ``x_real_ip_proxy``
        :rtype: werkzeug.wrappers.Request
        """
        if installed:
            self.assertTrue(
                real_ip.install(self._config(proxy=proxy), self.http),
            )
        return self._apply(_environ(**kw))

    def _master(self, **kw):
        """Return what the master records, its proxy named as deployed."""
        self._docker_answers(PROXY)
        return self._record(proxy=PROXY_NAME, **kw)

    def _resolved(self, request, ranges):
        """Return what ``client_ip()`` makes of *request*."""
        return trusted_proxies.client_ip(
            request.remote_addr, list(request.access_route), ranges,
        )

    # Core, self-hosted, with no CDN in front.

    def test_core_behind_its_own_proxy_unconfigured(self):
        """Nothing configured: Odoo's own reading, untouched."""
        self.assertFalse(real_ip.install(self._config(header=""), self.http))
        request = self._record(installed=False, chain=VISITOR, claimed=None)
        self.assertEqual(request.remote_addr, VISITOR)

    def test_core_behind_its_own_proxy_configured_anyway(self):
        """A CDN declared but not there: the last hop is the visitor
        the proxy accepted, so the chain is left as it came."""
        request = self._record(chain=VISITOR, claimed=None)
        self.assertEqual(request.remote_addr, VISITOR)

    def test_core_reached_directly(self):
        """No proxy at all: no chain, and the socket's address stays."""
        environ = _environ(remote=VISITOR, chain="", claimed=None)
        for key in ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_HOST",
                    "HTTP_X_FORWARDED_PROTO"):
            del environ[key]
        self.assertTrue(real_ip.install(self._config(), self.http))
        self.assertEqual(self._apply(environ).remote_addr, VISITOR)

    # Core, self-hosted, behind a CDN — without naming its proxy, then
    # naming it.

    def test_core_behind_a_cdn_its_proxy_trusts(self):
        self.assertEqual(self._record().remote_addr, VISITOR)

    def test_core_behind_a_cdn_its_proxy_does_not_trust(self):
        self.assertEqual(self._record(chain=EDGE).remote_addr, VISITOR)

    def test_core_behind_a_cdn_straight_into_odoo(self):
        self.assertEqual(
            self._record(remote=EDGE, chain=VISITOR).remote_addr, VISITOR,
        )

    def test_core_unnamed_lets_a_private_neighbour_choose(self):
        """The limit of the fallback, as ``proxy_mode`` already had it:
        on a network shared with other containers, name the proxy."""
        request = self._record(remote=NEIGHBOUR, chain=f"{FORGED}, {EDGE}",
                               claimed=FORGED)
        self.assertEqual(request.remote_addr, FORGED)

    def test_core_unnamed_stops_a_caller_from_outside_choosing(self):
        """Odoo's port open to the internet: ``proxy_mode`` alone would
        record whatever the caller put last."""
        request = self._record(remote=OUTSIDER, chain=FORGED, claimed=None)
        self.assertEqual(request.remote_addr, OUTSIDER)

    def test_core_named_records_the_visitor(self):
        self.assertEqual(self._record(proxy=PROXY).remote_addr, VISITOR)

    def test_core_named_stops_a_private_neighbour_choosing(self):
        request = self._record(proxy=PROXY, remote=NEIGHBOUR,
                               chain=f"{FORGED}, {EDGE}", claimed=FORGED)
        self.assertEqual(request.remote_addr, NEIGHBOUR)

    # The master, on Cloud1: Traefik trusts Cloudflare's ranges and is
    # named in its configuration.

    def test_master_records_the_visitor(self):
        request = self._master()
        self.assertEqual(request.remote_addr, VISITOR)
        self.assertEqual(request.access_route, [VISITOR])

    def test_master_client_ip_finds_the_visitor_before_and_after(self):
        """Every limit and audit line keyed on ``client_ip()``."""
        before = self._record(installed=False)
        self.assertEqual(before.remote_addr, EDGE)
        self.assertEqual(self._resolved(before, self.RANGES), VISITOR)
        after = self._master()
        self.assertEqual(self._resolved(after, self.RANGES), VISITOR)

    def test_master_still_sees_the_tenant_host_that_calls_it(self):
        """``requester_ip`` of a VPS order: the provider's firewall is
        opened to it, so it must stay the tenant's server."""
        chain = f"{TENANT_HOST}, {EDGE}"
        before = self._record(installed=False, chain=chain,
                              claimed=TENANT_HOST)
        self.assertEqual(self._resolved(before, self.RANGES), TENANT_HOST)
        after = self._master(chain=chain, claimed=TENANT_HOST)
        self.assertEqual(after.remote_addr, TENANT_HOST)
        self.assertEqual(self._resolved(after, self.RANGES), TENANT_HOST)

    def test_master_reached_by_a_host_off_the_cdn(self):
        """A platform host calling the master straight, not through the
        CDN: the host's own address, whatever header it carries."""
        request = self._master(chain=TENANT_HOST, claimed=FORGED)
        self.assertEqual(request.remote_addr, TENANT_HOST)

    def test_master_a_neighbour_with_the_header_cannot_choose(self):
        """A QA container, or any other on the shared network, calling
        the master's Odoo directly."""
        request = self._master(remote=NEIGHBOUR, chain=f"{FORGED}, {EDGE}",
                               claimed=FORGED)
        self.assertEqual(request.remote_addr, NEIGHBOUR)
        self.assertEqual(self._resolved(request, self.RANGES), NEIGHBOUR)

    def test_master_a_neighbour_with_only_a_chain_cannot_choose(self):
        """``X-Forwarded-For`` and ``X-Forwarded-Host`` are all Odoo's
        own ``proxy_mode`` needs to believe the last entry."""
        request = self._master(remote=NEIGHBOUR, chain=FORGED, claimed=None)
        self.assertEqual(request.remote_addr, NEIGHBOUR)

    def test_master_with_its_proxy_unresolvable_is_as_if_unnamed(self):
        """Not one address for everybody: ``client_ip()`` would key the
        sign-up, OIDC and ``/saas/*`` limits — and Odoo its sign-in
        lock-out — on the proxy alone, so one caller could lock everyone
        out. The weaker guarantee instead, and the log says so."""
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_NONAME")
        with self.assertLogs(LOGGER, "WARNING"):
            request = self._record(proxy=PROXY_NAME)
        self.assertEqual(request.remote_addr, VISITOR)
        self.assertEqual(self._resolved(request, self.RANGES), VISITOR)
        self.assertEqual(self._resolved(request, []), VISITOR)

    def test_devel_with_the_master_s_configuration_is_as_if_unnamed(self):
        """Devel loads the production configuration, and no container of
        that name exists there: Docker's resolver answers that it cannot
        say. Behaves as it did before the name was written."""
        resolver = self._docker_answers()
        resolver.side_effect = _gaierror("EAI_AGAIN")
        with self.assertLogs(LOGGER, "WARNING"):
            request = self._record(proxy=PROXY_NAME, chain=VISITOR,
                                   claimed=None)
        self.assertEqual(request.remote_addr, VISITOR)

    # A tenant's panel: the same posture, rendered by the deploy (see
    # the instance tests below), and Settings that declare no proxy, so
    # ``client_ip()`` is whatever Odoo recorded.

    def test_tenant_panel_client_ip_is_the_visitor(self):
        request = self._master()
        self.assertEqual(request.remote_addr, VISITOR)
        self.assertEqual(self._resolved(request, []), VISITOR)

    def test_tenant_panel_a_neighbouring_panel_cannot_choose(self):
        request = self._master(remote=NEIGHBOUR, chain=FORGED, claimed=None)
        self.assertEqual(self._resolved(request, []), NEIGHBOUR)

    # Callers that would like to be recorded as somebody else.

    def test_a_caller_through_our_proxy_off_the_cdn_cannot_choose(self):
        request = self._master(chain=OUTSIDER, claimed=FORGED)
        self.assertEqual(request.remote_addr, OUTSIDER)

    def test_a_caller_reaching_odoo_from_outside_cannot_choose(self):
        """Odoo's port open to the internet, with the chain and the
        header our proxy would have sent."""
        request = self._record(remote=OUTSIDER, chain=f"{FORGED}, {EDGE}",
                               claimed=FORGED)
        self.assertEqual(request.remote_addr, OUTSIDER)


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


class TestWhatAnInstanceIsConfiguredWith(
    _DockerAnswers, _OnTheUncorrectedProxyFix, TransactionCase,
):
    """The instance is where this has to arrive: the correction runs
    inside the panel's own Odoo, so the panel's own configuration is
    what has to name the header, say whose word to take for it, and say
    which container is our proxy.
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

    def _options(self):
        """Return the rendered ``[options]``, parsed."""
        parser = configparser.ConfigParser()
        parser.read_string(self._conf())
        return parser["options"]

    def _behind_the_cdn(self):
        """Have the fixture host state a header and the CDN's ranges."""
        self.host.write({
            "client_ip_header": "CF-Connecting-IP",
            "trusted_proxy_ranges": "173.245.48.0/20\n2400:cb00::/32",
        })

    def test_the_proxy_setting_the_platform_owns_is_still_forced(self):
        self.assertIn("proxy_mode = True", self._conf())

    def test_a_host_stating_nothing_configures_nothing(self):
        conf = self._conf()
        self.assertNotIn(real_ip.HEADER_OPTION, conf)
        self.assertNotIn(real_ip.TRUSTED_OPTION, conf)
        self.assertNotIn(PROXY_OPTION, conf)

    def test_both_halves_travel_together(self):
        self._behind_the_cdn()
        conf = self._conf()
        self.assertIn("CF-Connecting-IP", conf)
        self.assertIn("173.245.48.0/20", conf)
        self.assertIn("2400:cb00::/32", conf)

    def test_the_host_s_proxy_is_named_with_them(self):
        """By the container Full Setup runs it as, never by the alias
        ``proxy``, which any compose project on the network may use."""
        self._behind_the_cdn()
        self.assertEqual(self._options().get(PROXY_OPTION), PROXY_NAME)

    def test_a_header_with_nobody_to_believe_is_not_written(self):
        """One believed from anywhere is one any visitor can write."""
        self.host.client_ip_header = "CF-Connecting-IP"
        self.assertNotIn(real_ip.HEADER_OPTION, self._conf())

    def test_it_is_cleared_when_the_host_stops_stating_one(self):
        """Moving an instance to a host reached directly must not leave
        it believing a header nobody sets."""
        self._behind_the_cdn()
        self.instance.odoo_conf = self._conf()
        self.host.client_ip_header = ""
        conf = self._conf()
        self.assertNotIn(real_ip.HEADER_OPTION, conf)
        self.assertNotIn("CF-Connecting-IP", conf)
        self.assertNotIn(PROXY_OPTION, conf)

    def test_what_it_is_configured_with_records_the_visitor(self):
        """Read back as the panel's own Odoo reads it, with Docker
        answering for the proxy's name. Its Settings declare no proxy,
        so ``client_ip()`` there is whatever Odoo recorded."""
        self._behind_the_cdn()
        self._docker_answers(PROXY)
        self.assertTrue(real_ip.install(self._options(), self.http))
        request = self._apply(_environ())
        self.assertEqual(request.remote_addr, VISITOR)
        self.assertEqual(
            trusted_proxies.client_ip(
                request.remote_addr, list(request.access_route), [],
            ),
            VISITOR,
        )
        neighbour = self._apply(
            _environ(remote=NEIGHBOUR, chain=FORGED, claimed=FORGED),
        )
        self.assertEqual(neighbour.remote_addr, NEIGHBOUR)


class TestACustomerInstance(BaseCase):
    """A customer's own Odoo gets the options and nothing that reads them.

    The deploy writes them into every instance's configuration, but the
    correction lives in this module, and a customer's instance is only
    given ``incubacloud_connect``. Pinned so that changing it is a
    decision, made with the documentation that says so.
    """

    def test_the_module_it_is_given_installs_no_correction(self):
        self.assertFalse(get_manifest("incubacloud_connect").get("post_load"))

    def test_nothing_it_is_given_reads_the_options(self):
        root = pathlib.Path(get_module_path("incubacloud_connect"))
        sources = "".join(path.read_text() for path in root.rglob("*.py"))
        for option in (real_ip.HEADER_OPTION, real_ip.TRUSTED_OPTION,
                       PROXY_OPTION, "real_ip"):
            with self.subTest(option=option):
                self.assertNotIn(option, sources)
