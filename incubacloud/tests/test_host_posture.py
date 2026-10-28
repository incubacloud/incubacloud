"""A host said to be closed is checked, not assumed.

``block_direct_access`` has two halves: Traefik refusing what skipped
the proxies, and the firewall dropping it before it gets there. The
second only lands with a hardening run, so the box can say "closed"
while the origin answers everybody. On 27 October 2026 a host behind
the CDN had been open at its origin for nineteen days, and no host had
taken the hardening changes of the twelve days before; nothing in the
panel said so.

These tests pin the two questions the hourly check asks: does a host
said to admit only its proxies really drop a direct connection
(``host_origin_open``, measured, confirmed over two runs), and is its
hardening the recipe and the posture the panel wants now
(``host_hardening_outdated``, read from the records).

The network is real for the probe itself (a socket on this machine) and
stood in for, with ``autospec``, where what is under test is the
judgement.
"""
import asyncio
import errno
import hashlib
import importlib
import importlib.util
import os
import socket
import socketserver
import threading
from unittest.mock import MagicMock, patch

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.host_hardening_executor import (
    HostHardeningExecutor,
)
from odoo.addons.incubacloud.models.registry import executor_registry

_POSTURE = "odoo.addons.incubacloud.models.cloud_host_posture"
_HOST_IP = "203.0.113.20"
_NAMELESS_IP = "203.0.113.30"
_THROUGH_THE_CDN = "HTTP/1.1 200 OK"
ORIGIN_OPEN = "host_origin_open"
OUTDATED = "host_hardening_outdated"


def _posture():
    """Import the posture module (a test fails here while it is missing)."""
    return importlib.import_module(_POSTURE)


class PostureCase(TransactionCase):
    """A host behind the CDN, closed to direct traffic, its proxy up."""

    def setUp(self):
        super().setUp()
        self.Alert = self.env["cloud.alert"].sudo()
        self.Host = self.env["cloud.host"]
        self.host = self.Host.create({
            "name": "posture-host",
            "ip_address": _HOST_IP,
            "user": "root",
            "wildcard_domain": "posture.test",
            "trusted_proxy_ranges": "198.51.100.0/24\n2001:db8::/32",
            "behind_cdn": True,
            "block_direct_access": True,
            "traefik_deployed": True,
        })

    def _alert(self, code, host=None):
        """Return the active alert *code* about *host* (this test's host)."""
        return self.Alert.search([
            ("code", "=", code),
            ("state", "=", "active"),
            ("host_id", "=", (host or self.host).id),
        ])


class TestOriginProbe(PostureCase):
    """The panel connects straight to the host, as a visitor bypassing
    the CDN would, and is told when anything lets it in. A host that
    drops it reads as closed only once the same name answers through
    the CDN."""

    def setUp(self):
        super().setUp()
        project = self.env["cloud.project"].create({"name": "posture-proj"})
        self.name = self.env["cloud.instance"].create({
            "name": "posture-inst",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
        }).domain
        self.assertTrue(self.name)
        self.assertNotEqual(self.name, _HOST_IP)

    def _run(self, answers=None, cdn=_THROUGH_THE_CDN, own_address=True):
        """Run the origin check against stand-in answers, nothing on the wire.

        :param dict answers: ``{port: answer}`` at a host's own address;
            a port left out drops the connection (``""``)
        :param cdn: what the host's name answers through the CDN on 443:
            a status line when reached, ``""`` or ``None`` when not
        :param bool own_address: whether a direct connection to this
            panel's own address goes through, the control the check once
            made for every host; nothing else connects directly
        :return: the stand-in for ``_origin_answer``, to read its calls
        """
        answers = answers or {}

        def answer(ip, port, name, tls, **kw):
            """Answer as the origin at an address, as the CDN at the name."""
            if ip in (_HOST_IP, _NAMELESS_IP):
                return answers.get(port, "")
            if (ip, port, name, tls) == (self.name, 443, self.name, True):
                return cdn
            return ""

        direct = (
            {"return_value": MagicMock(spec=socket.socket)} if own_address
            else {"side_effect": ConnectionRefusedError(errno.ECONNREFUSED, "")}
        )
        with patch(
            f"{_POSTURE}._origin_answer", autospec=True, side_effect=answer,
        ) as probe, patch.object(
            socket, "create_connection", autospec=True, **direct,
        ):
            self.Host._check_origin_closed()
        return probe

    def _run_confirmed(self, answers, **kw):
        """Twice in a row: ``host_origin_open`` is a measurement."""
        self._run(answers, **kw)
        return self._run(answers, **kw)

    def _nameless(self):
        """Create a host said to be closed that serves no name yet."""
        return self.Host.create({
            "name": "posture-nameless",
            "ip_address": _NAMELESS_IP,
            "user": "root",
            "wildcard_domain": "posture.test",
            "trusted_proxy_ranges": "198.51.100.0/24",
            "behind_cdn": True,
            "block_direct_access": True,
            "traefik_deployed": True,
        })

    def test_a_closed_origin_raises_nothing(self):
        self._run_confirmed({})
        self.assertFalse(self._alert(ORIGIN_OPEN))

    def test_one_answer_is_a_suspicion_not_an_alert(self):
        self._run({443: "HTTP/1.1 200 OK"})
        self.assertFalse(self._alert(ORIGIN_OPEN))

    def test_an_origin_answering_twice_raises_the_alert(self):
        self._run_confirmed({443: "HTTP/1.1 403 Forbidden"})
        alert = self._alert(ORIGIN_OPEN)
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")
        self.assertEqual(alert.audience, "fleet")
        # What answered, where, and the step that closes it.
        for part in ("posture-host", _HOST_IP, "443", "403 Forbidden",
                     "Re-apply Hardening"):
            with self.subTest(part=part):
                self.assertIn(part, alert.message)

    def test_port_80_is_asked_too(self):
        """A visitor bypassing the CDN may well start with plain HTTP."""
        self._run_confirmed({80: "HTTP/1.1 308 Permanent Redirect"})
        alert = self._alert(ORIGIN_OPEN)
        self.assertIn("port 80", alert.message)

    def test_it_asks_for_a_name_the_host_serves(self):
        """A request to the bare address proves nothing about what a
        visitor of the host's instances would get."""
        calls = [(c.args, c.kwargs) for c in self._run().call_args_list]
        self.assertIn(((_HOST_IP, 443, self.name, True), {}), calls)
        self.assertIn(((_HOST_IP, 80, self.name, False), {}), calls)

    def test_a_host_serving_no_name_is_asked_by_its_address(self):
        self._nameless()
        self.assertIn(
            (_NAMELESS_IP, 443, _NAMELESS_IP, True),
            [c.args for c in self._run().call_args_list],
        )

    def test_the_alert_clears_once_the_origin_stops_answering(self):
        """Dropped at the origin, and the same name reached through the
        CDN on 443: closed."""
        self._run_confirmed({443: "HTTP/1.1 200 OK"})
        alert = self._alert(ORIGIN_OPEN)
        self.assertTrue(alert)
        probe = self._run({})
        self.assertIn(
            (self.name, 443, self.name, True),
            [c.args for c in probe.call_args_list],
        )
        self.assertFalse(self._alert(ORIGIN_OPEN))
        self.assertEqual(alert.resolution, "auto")

    def test_with_the_cdn_out_of_reach_a_dropped_origin_is_no_reading(self):
        """Silence is the healthy reading here: a panel whose own egress
        drops everything -- out only through a proxy, or inside a
        perimeter that discards direct connections while its own name
        resolves within -- would read every host as closed and clear
        every alert. The host's name reached through the CDN tells the
        two apart, whatever the panel's own address does."""
        self._run_confirmed({443: "HTTP/1.1 403 Forbidden"})
        alert = self._alert(ORIGIN_OPEN)
        self.assertTrue(alert)
        for cdn in ("", None):
            with self.subTest(cdn=cdn):
                self._run({}, cdn=cdn)
                self.assertEqual(alert.state, "active")

    def test_with_the_cdn_out_of_reach_the_first_sighting_is_kept(self):
        self._run({443: "HTTP/1.1 200 OK"})
        self._run({}, cdn="")
        self.assertFalse(self._alert(ORIGIN_OPEN))
        self._run({443: "HTTP/1.1 200 OK"})
        self.assertTrue(self._alert(ORIGIN_OPEN))

    def test_an_origin_letting_it_in_needs_no_cdn(self):
        """Open is read at the origin alone: the CDN is asked only to
        tell a drop from a blind panel."""
        probe = self._run({443: "HTTP/1.1 403 Forbidden"}, cdn="")
        self._run({443: "HTTP/1.1 403 Forbidden"}, cdn="")
        self.assertTrue(self._alert(ORIGIN_OPEN))
        self.assertNotIn(self.name, [c.args[0] for c in probe.call_args_list])

    def test_a_host_serving_no_name_gets_no_reading_from_a_drop(self):
        """No name to reach it by through the CDN, so nothing tells its
        drop from a blind panel's; an answer is still open."""
        nameless = self._nameless()
        self._run_confirmed({443: "HTTP/1.1 403 Forbidden"})
        alert = self._alert(ORIGIN_OPEN, nameless)
        self.assertTrue(alert)
        probe = self._run({})
        self.assertEqual(alert.state, "active")
        self.assertEqual(
            [c.args for c in probe.call_args_list if _NAMELESS_IP in c.args],
            [(_NAMELESS_IP, 443, _NAMELESS_IP, True),
             (_NAMELESS_IP, 80, _NAMELESS_IP, False)],
        )

    def test_the_panels_own_address_plays_no_part(self):
        """It was the control once, on 443: a panel served on another
        port or over plain HTTP never decided anything, and one whose
        name resolved inside a perimeter passed while every host read
        as closed."""
        self._run_confirmed({443: "HTTP/1.1 403 Forbidden"}, own_address=False)
        alert = self._alert(ORIGIN_OPEN)
        self.assertTrue(alert)
        self._run({}, own_address=False)
        self.assertEqual(alert.resolution, "auto")

    def test_a_refused_or_unreachable_origin_neither_raises_nor_clears(self):
        """A reset or an unreachable address is no reading. Taken as
        closed, an open host whose proxy was restarting -- under the
        Push Trusted Proxy Settings the alert itself asks for, or a Full
        Setup -- had its alert cleared, and one whose proxy was down was
        never told: its firewall lets the SYN in, the kernel resets it."""
        self._run_confirmed({443: "HTTP/1.1 403 Forbidden"})
        alert = self._alert(ORIGIN_OPEN)
        self.assertTrue(alert)
        self._run({443: None, 80: None})
        self.assertEqual(alert.state, "active")
        # One port without a reading and the other dropping it decides
        # nothing either: the first may well be the open one.
        self._run({443: None})
        self.assertEqual(alert.state, "active")

    def test_a_run_without_a_reading_keeps_the_first_sighting(self):
        """It does not count as a clear reading between two sightings."""
        self._run({443: "HTTP/1.1 200 OK"})
        self._run({443: None, 80: None})
        self.assertFalse(self._alert(ORIGIN_OPEN))
        self._run({443: "HTTP/1.1 200 OK"})
        self.assertTrue(self._alert(ORIGIN_OPEN))

    def test_a_port_without_a_reading_does_not_hide_one_that_answers(self):
        self._run_confirmed({443: None, 80: "HTTP/1.1 308 Permanent Redirect"})
        self.assertIn("port 80", self._alert(ORIGIN_OPEN).message)

    def test_hosts_not_said_to_be_closed_are_not_asked(self):
        """Behind the CDN and still open (the state a pool host is in
        between moving its names and closing its firewall), not set up
        yet, or not provisioned yet: nothing claims they are closed."""
        base = {"user": "root", "wildcard_domain": "posture.test", "behind_cdn": True}
        open_ = self.Host.create({
            **base, "name": "posture-open", "ip_address": "203.0.113.21",
            "traefik_deployed": True,
        })
        unset = self.Host.create({
            **base, "name": "posture-unset", "ip_address": "203.0.113.22",
            "block_direct_access": True,
        })
        unbought = self.Host.create({
            **base, "name": "posture-unbought", "ip_address": "0.0.0.0",
            "block_direct_access": True, "traefik_deployed": True,
        })
        asked = {c.args[0] for c in self._run().call_args_list}
        self.assertIn(_HOST_IP, asked)
        for host in (open_, unset, unbought):
            with self.subTest(host=host.name):
                self.assertNotIn(host.ip_address, asked)

    def test_a_host_no_longer_said_to_be_closed_has_its_alert_cleared(self):
        """Nothing will ask it again, so a lit alert would stay lit."""
        self._run_confirmed({443: "HTTP/1.1 200 OK"})
        self.assertTrue(self._alert(ORIGIN_OPEN))
        self.host.block_direct_access = False
        probe = self._run({443: "HTTP/1.1 200 OK"})
        self.assertNotIn(_HOST_IP, {c.args[0] for c in probe.call_args_list})
        self.assertFalse(self._alert(ORIGIN_OPEN))

    def test_a_host_no_longer_said_to_be_closed_starts_over(self):
        """Its first sighting is forgotten too: back in scope later, it
        is counted from one again, not confirmed by an old reading."""
        self._run({443: "HTTP/1.1 200 OK"})
        Suspicion = self.env["cloud.alert.suspicion"].sudo()
        domain = [("code", "=", ORIGIN_OPEN), ("host_id", "=", self.host.id)]
        self.assertTrue(Suspicion.search(domain))
        self.host.block_direct_access = False
        self._run({443: "HTTP/1.1 200 OK"})
        self.assertFalse(Suspicion.search(domain))

    def test_two_runs_three_hours_apart_confirm(self):
        """Hourly, so a run the scheduler delayed still counts."""
        self.assertEqual(
            self.env["cloud.alert.policy"]._for_code(ORIGIN_OPEN), (2, 10800),
        )

    def test_the_cron_asks_both_questions_hourly(self):
        cron = self.env.ref("incubacloud.cron_check_host_posture")
        self.assertEqual(cron.code.strip(), "model._cron_check_posture()")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "hours"))
        with patch.object(
            type(self.Host), "_check_hardening_current", autospec=True,
        ) as hardening, patch.object(
            type(self.Host), "_check_origin_closed", autospec=True,
        ) as origin:
            self.Host._cron_check_posture()
        hardening.assert_called_once()
        origin.assert_called_once()


class _Answer(socketserver.BaseRequestHandler):
    """Read one request and answer it with the server's ``head``."""

    def handle(self):
        """Keep what was asked, answer, close."""
        self.server.asked = self.request.recv(4096)
        self.request.sendall(self.server.head)


def _serve_once(case, head):
    """Start a TCP server on 127.0.0.1 that answers *head* to one request.

    It gives up after two seconds unasked, so a test whose code never
    calls it does not wait on it.

    :param case: the test, which closes the server when it ends
    :param bytes head: what to answer
    :return: the server; its port is ``server_address[1]``, and what it
        was asked is kept in ``asked``
    """
    server = socketserver.TCPServer(("127.0.0.1", 0), _Answer)
    server.timeout = 2
    server.head = head
    server.asked = b""
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    case.addCleanup(server.server_close)
    case.addCleanup(thread.join, 5)
    return server


def _dropping_port(case):
    """Return a port on 127.0.0.1 that drops new connections unanswered.

    A listener whose accept queue holds one connection, filled at once:
    Linux drops a SYN that finds the queue full, rather than resetting
    it, so a connection to it waits until its timeout runs out -- what
    a firewall that admits only the CDN does to everybody else.

    :param case: the test, which closes both sockets when it ends
    :rtype: int
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    case.addCleanup(listener.close)
    port = listener.getsockname()[1]
    filling = socket.create_connection(("127.0.0.1", port), timeout=2)
    case.addCleanup(filling.close)
    return port


class TestOriginAnswer(TransactionCase):
    """The probe itself, against sockets on this machine."""

    def test_a_dropped_connection_is_no_answer(self):
        """No reply at all, until the wait runs out: only that reads as
        closed."""
        self.assertEqual(
            _posture()._origin_answer(
                "127.0.0.1", _dropping_port(self), "x.test", True, timeout=0.5,
            ),
            "",
        )

    def test_a_refused_connection_is_no_reading(self):
        """Nothing listening resets the connection: the firewall let it
        through to a port nobody serves, as when the proxy restarts or
        is down. Neither the drop of a closed origin nor an answer."""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertIsNone(
            _posture()._origin_answer("127.0.0.1", port, "x.test", False, timeout=2),
        )

    def test_an_unreachable_address_is_no_reading(self):
        """A router saying the address cannot be reached says nothing of
        the host's firewall."""
        with patch.object(
            socket, "create_connection", autospec=True,
            side_effect=OSError(errno.EHOSTUNREACH, "No route to host"),
        ) as connect:
            answer = _posture()._origin_answer(_HOST_IP, 443, "x.test", True)
        connect.assert_called_once()
        self.assertIsNone(answer)

    def test_a_server_letting_it_in_answers_its_status_line(self):
        server = _serve_once(
            self, b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
        )
        answer = _posture()._origin_answer(
            "127.0.0.1", server.server_address[1], "shop.posture.test", False,
            timeout=2,
        )
        self.assertEqual(answer, "HTTP/1.1 403 Forbidden")
        # Asked as a visitor of that name would, not by the address.
        self.assertIn(b"Host: shop.posture.test\r\n", server.asked)

    def test_a_connection_let_in_is_an_answer_whatever_follows(self):
        """TLS refused after the connection got in is still an origin
        that lets anybody connect."""
        server = _serve_once(self, b"not tls\r\n")
        answer = _posture()._origin_answer(
            "127.0.0.1", server.server_address[1], "x.test", True, timeout=2,
        )
        self.assertTrue(answer)


class TestOriginThroughTheCdn(PostureCase):
    """The probe end to end, every connection it makes stood in for:
    the host's address drops it, and its name is reached -- or not --
    at a server on this machine. Nothing leaves it."""

    NAME = "shop.posture.test"

    def _probe(self, cdn_reached):
        """Run ``_probe_origin`` on the host, serving :attr:`NAME`.

        :param bool cdn_reached: whether the name answers through the CDN
        :return: ``(result, addresses connected to, in order)``
        """
        connect = socket.create_connection
        server = _serve_once(self, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        def direct(address, timeout=None, *args, **kwargs):
            """Drop the origin; reach the name at the local server, or not."""
            if address[0] == _HOST_IP or not cdn_reached:
                raise TimeoutError("timed out")
            return connect(("127.0.0.1", server.server_address[1]), timeout=timeout)

        with patch.object(
            type(self.Host), "_origin_probe_name", autospec=True,
            return_value=self.NAME,
        ), patch.object(
            socket, "create_connection", autospec=True, side_effect=direct,
        ) as made:
            result = self.host._probe_origin()
        return result, [c.args[0] for c in made.call_args_list]

    def test_dropped_at_the_origin_and_reached_through_the_cdn_is_closed(self):
        result, addresses = self._probe(cdn_reached=True)
        self.assertIsNone(result)
        self.assertEqual(
            addresses, [(_HOST_IP, 443), (_HOST_IP, 80), (self.NAME, 443)],
        )

    def test_dropped_at_the_origin_with_the_cdn_out_of_reach_is_no_reading(self):
        result, addresses = self._probe(cdn_reached=False)
        self.assertEqual(result, (443, self.NAME, None))
        self.assertEqual(addresses[-1], (self.NAME, 443))


class TestHardeningCurrent(PostureCase):
    """The hardening a host runs is compared with what the panel wants:
    the recipe it ships, and the posture the host is set to."""

    def setUp(self):
        super().setUp()
        self.recipe = executor_registry.get("host_hardening")._recipe_fingerprint()
        self.host.write({
            "hardened": True,
            "hardening_recipe": self.recipe,
            "hardening_cdn_only": True,
        })

    def _run(self):
        self.Host._check_hardening_current()
        return self._alert(OUTDATED)

    def test_a_host_hardened_with_this_recipe_and_posture_raises_nothing(self):
        self.assertFalse(self._run())

    def test_an_older_recipe_raises_the_alert(self):
        self.host.hardening_recipe = "0123456789ab"
        alert = self._run()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")
        self.assertEqual(alert.audience, "fleet")
        for part in ("posture-host", "0123456789ab", self.recipe,
                     "Re-apply Hardening"):
            with self.subTest(part=part):
                self.assertIn(part, alert.message)

    def test_a_host_hardened_before_the_recipe_was_recorded_raises_it(self):
        """Every host hardened before this check existed: what it runs is
        unknown, so it is not assumed current."""
        self.host.hardening_recipe = False
        self.assertIn("Re-apply Hardening", self._run().message)

    def test_a_host_closed_after_its_hardening_raises_it(self):
        """Ticked by hand, or by Full Setup on a host joining a pool after
        its hardening: Traefik refuses, the firewall still lets in."""
        self.host.hardening_cdn_only = False
        alert = self._run()
        self.assertIn("80 and 443", alert.message)
        self.assertIn("Re-apply Hardening", alert.message)

    def test_a_host_reopened_after_its_hardening_raises_it(self):
        """Taken off the CDN, its firewall still drops every visitor."""
        self.host.write({"behind_cdn": False, "block_direct_access": False})
        alert = self._run()
        self.assertIn("80 and 443", alert.message)

    def test_a_closed_host_without_ranges_is_not_asked_to_close(self):
        """With no range to admit, hardening leaves 80/443 open whatever
        the box says, so recording it open is current."""
        self.host.write({
            "trusted_proxy_ranges": False, "hardening_cdn_only": False,
        })
        with patch.object(
            type(self.Host), "_effective_trusted_proxy_ranges", autospec=True,
            return_value=[],
        ):
            self.assertFalse(self._run())

    def test_a_host_never_hardened_from_the_panel_is_not_judged(self):
        self.host.write({"hardened": False, "hardening_recipe": False})
        self.assertFalse(self._run())

    def test_the_alert_clears_once_hardened_again(self):
        self.host.hardening_recipe = "0123456789ab"
        alert = self._run()
        self.assertTrue(alert)
        self.host.hardening_recipe = self.recipe
        self.assertFalse(self._run())
        self.assertEqual(alert.resolution, "auto")

    def test_an_archived_host_has_its_alert_cleared(self):
        self.host.hardening_recipe = "0123456789ab"
        self.assertTrue(self._run())
        self.host.active = False
        self.Host._check_hardening_current()
        self.assertFalse(self._alert(OUTDATED))


class TestWhetherHardeningAdmitsOnlyProxies(PostureCase):
    """One method of the host answers it, for the run that renders the
    firewall and for the check that compares what a run recorded."""

    V4, V6 = "198.51.100.0/24", "2001:db8::/32"

    def _admits(self, behind, block, ranges):
        """Ask the host, its effective proxy ranges stood in for.

        :param list ranges: what ``_effective_trusted_proxy_ranges``
            returns -- the host's own field, or what a layer supplies
        :rtype: bool
        """
        self.host.write({"behind_cdn": behind, "block_direct_access": block})
        with patch.object(
            type(self.Host), "_effective_trusted_proxy_ranges", autospec=True,
            return_value=ranges,
        ):
            return self.host._hardening_admits_only_proxies()

    def _rendered(self, admits):
        """Return what a hardening run renders while the host answers *admits*.

        :rtype: dict
        """
        executor = HostHardeningExecutor.__new__(HostHardeningExecutor)
        executor.job = self.env["cloud.job"].new({"host_id": self.host.id})
        with patch.object(
            type(self.Host), "_hardening_admits_only_proxies", autospec=True,
            return_value=admits,
        ):
            return executor._http_allowlist_vars()

    def test_behind_a_cdn_refusing_direct_traffic_with_an_ipv4_range(self):
        """All three: an allowlist of IPv6 ranges alone would shut every
        IPv4 visitor out, so the firewall stays open then."""
        for behind in (False, True):
            for block in (False, True):
                for ranges in ([], [self.V6], [self.V4], [self.V4, self.V6]):
                    with self.subTest(behind=behind, block=block, ranges=ranges):
                        self.assertIs(
                            self._admits(behind, block, ranges),
                            behind and block and self.V4 in ranges,
                        )

    def test_the_run_renders_the_allowlist_when_the_host_says_so(self):
        """Whatever the flags look like: the run reads the host's answer,
        not a copy of its condition."""
        self.host.write({
            "behind_cdn": False, "block_direct_access": False,
            "trusted_proxy_ranges": f"{self.V4}\n{self.V6}",
        })
        self.assertEqual(self._rendered(True), {
            "ic_http_allowed_ranges": f"{self.V4}, {self.V6}",
            "ic_http_allowed_ranges_v4": self.V4,
            "ic_http_allowed_ranges_v6": self.V6,
        })

    def test_the_run_leaves_80_and_443_open_when_the_host_says_so(self):
        self.host.write({
            "behind_cdn": True, "block_direct_access": True,
            "trusted_proxy_ranges": f"{self.V4}\n{self.V6}",
        })
        self.assertEqual(self._rendered(False), {})


class TestRecipeFingerprint(TransactionCase):

    def _playbook_text(self):
        path = os.path.join(
            get_module_path("incubacloud"),
            "ansible", "playbooks", "host_hardening.yml",
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_rewording_an_explanation_changes_nothing(self):
        from odoo.addons.incubacloud.models.host_hardening_executor import (
            _recipe_fingerprint_of,
        )
        text = (
            "- name: x\n  # why\n  copy:\n"
            "    content: |\n      # file note\n      a b\n"
        )
        reworded = text.replace("# why", "# because").replace(
            "# file note", "# another note",
        ) + "\n\n"
        self.assertEqual(_recipe_fingerprint_of(text), _recipe_fingerprint_of(reworded))

    def test_changing_what_it_does_changes_it(self):
        from odoo.addons.incubacloud.models.host_hardening_executor import (
            _recipe_fingerprint_of,
        )
        text = "- name: x\n  copy:\n    content: |\n      a b\n"
        self.assertNotEqual(
            _recipe_fingerprint_of(text),
            _recipe_fingerprint_of(text.replace("a b", "a c")),
        )

    def test_it_is_the_shipped_playbooks(self):
        from odoo.addons.incubacloud.models.host_hardening_executor import (
            _recipe_fingerprint_of,
        )
        expected = _recipe_fingerprint_of(self._playbook_text())
        self.assertEqual(len(expected), 12)
        self.assertEqual(HostHardeningExecutor._recipe_fingerprint(), expected)
        # Whichever layer runs the job reads the same file.
        self.assertEqual(
            executor_registry.get("host_hardening")._recipe_fingerprint(),
            expected,
        )
        self.assertEqual(
            expected,
            hashlib.sha256("\n".join(
                line.rstrip() for line in self._playbook_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ).encode()).hexdigest()[:12],
        )


class TestHardeningRecordsWhatItApplied(TransactionCase):
    """A successful run says what it applied, which is what the check
    compares; a failed one leaves the record as it was."""

    def _host(self, **vals):
        return self.env["cloud.host"].create({
            "name": "applied-host",
            "ip_address": "10.0.0.9",
            "user": "ubuntu",
            "port": 22345,
            "wildcard_domain": "example.com",
            "trusted_proxy_ranges": "198.51.100.0/24",
        } | vals)

    def _run_on_success(self, host, proof=True):
        """Run ``on_success`` with the SSH steps stood in for.

        :return: the stand-in for ``_resolve_alert``
        """
        executor = object.__new__(HostHardeningExecutor)
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.host_id = host
        executor.env = self.env
        executor._log_buffer = []
        with patch.object(HostHardeningExecutor, "_sys", autospec=True), \
                patch.object(
                    HostHardeningExecutor, "_resolve_alert", autospec=True,
                ) as resolve, \
                patch.object(HostHardeningExecutor, "_alert", autospec=True), \
                patch.object(
                    HostHardeningExecutor, "_open_edge_firewall_port",
                    autospec=True, return_value=None,
                ), \
                patch.object(
                    HostHardeningExecutor, "_finalize_disable_root",
                    autospec=True, return_value=proof,
                ):
            asyncio.run(executor.on_success({}))
        return resolve

    def test_a_closed_host_records_the_recipe_and_the_closed_firewall(self):
        host = self._host(behind_cdn=True, block_direct_access=True)
        self._run_on_success(host)
        self.assertTrue(host.hardened)
        self.assertEqual(
            host.hardening_recipe, HostHardeningExecutor._recipe_fingerprint(),
        )
        self.assertIs(host.hardening_cdn_only, True)

    def test_an_open_host_records_an_open_firewall(self):
        host = self._host(behind_cdn=True, hardening_cdn_only=True)
        self._run_on_success(host)
        self.assertIs(host.hardening_cdn_only, False)

    def test_a_successful_run_clears_the_outdated_alert(self):
        resolve = self._run_on_success(self._host())
        codes = [c.args[1] for c in resolve.call_args_list]
        self.assertIn(OUTDATED, codes)
        # And still the failure it used to clear.
        self.assertIn("hardening_failed", codes)

    def test_a_run_whose_login_proof_fails_records_nothing(self):
        host = self._host(hardening_recipe="0123456789ab")
        with self.assertRaises(RuntimeError):
            self._run_on_success(host, proof=False)
        self.assertFalse(host.hardened)
        self.assertEqual(host.hardening_recipe, "0123456789ab")
        self.assertFalse(host.hardening_cdn_only)


class TestThePostureCronGoesToTheBot(TransactionCase):
    """A cron added in a later release lands owned by uid 1 on every
    existing database; the 1.0.200 post-migrate hands it to the bot."""

    def _migration(self):
        path = os.path.join(
            get_module_path("incubacloud"), "migrations", "1.0.200",
            "post-migrate.py",
        )
        spec = importlib.util.spec_from_file_location(
            "ic_post_migrate_1_0_200", path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def setUp(self):
        super().setUp()
        self.cron = self.env.ref("incubacloud.cron_check_host_posture").sudo()
        self.bot = self.env.ref("incubacloud.user_incubacloud_cron")
        self.cron.user_id = self.env.ref("base.user_root")

    def test_an_upgraded_database_gives_it_to_the_bot(self):
        self._migration().migrate(self.env.cr, "1.0.198")
        self.cron.invalidate_recordset()
        self.assertEqual(self.cron.user_id, self.bot)

    def test_a_fresh_install_is_left_to_the_install_hook(self):
        self._migration().migrate(self.env.cr, None)
        self.cron.invalidate_recordset()
        self.assertNotEqual(self.cron.user_id, self.bot)
