"""Where a user picks the destination, the manager decides who it dials.

Notification webhooks are the one outbound URL in this codebase that a
user chooses. The request leaves a machine that answers on
169.254.169.254:80, 127.0.0.1:8069 and db:5432 (measured in production
on 2026-08-22) with no egress filter behind it, and the old code
guarded that with ``url.startswith('https://')`` at save time before
handing the URL to a bare ``urllib.request.urlopen``.

These tests pin what replaced it: the address families that must never
be dialled, the URL shapes that hide one address behind another, the
refusal to follow a redirect out of the checked destination, and — the
part that makes the rest mean anything — that the socket goes to the
address that was validated rather than to whatever DNS says on the
second lookup.
"""
import ipaddress
import json
import socket
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase

from odoo.addons.incubacloud.net.outbound import (
    OutboundError,
    _PinnedHTTPSConnection,
    _reject_address,
    post_json,
    validate_url,
)


def _addrinfo(*ips):
    """Build a ``getaddrinfo`` return value for *ips*.

    :param ips: address strings the fake resolver should answer with.
    :returns: list shaped like the real ``socket.getaddrinfo`` result.
    """
    return [
        (
            socket.AF_INET6 if ':' in ip else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            '',
            (ip, 443),
        )
        for ip in ips
    ]


def _resolving_to(*ips):
    """Patch DNS so any hostname resolves to *ips*."""
    return patch(
        'odoo.addons.incubacloud.net.outbound.getaddrinfo',
        return_value=_addrinfo(*ips),
    )


class TestRejectedAddresses(BaseCase):
    """Every family that must never be reached from the manager."""

    def test_the_dangerous_families_are_named_and_refused(self):
        for raw, expected in (
            ('127.0.0.1', 'loopback'),
            ('::1', 'loopback'),
            ('169.254.169.254', 'link-local'),   # cloud metadata
            ('fe80::1', 'link-local'),
            ('10.0.0.5', 'private'),
            ('172.16.0.1', 'private'),
            ('192.168.1.1', 'private'),
            ('fd00::1', 'private'),              # IPv6 ULA
            ('0.0.0.0', 'unspecified'),
            ('224.0.0.1', 'multicast'),
        ):
            reason = _reject_address(ipaddress.ip_address(raw))
            self.assertTrue(reason, f'{raw} was allowed')
            self.assertIn(expected, reason, raw)

    def test_an_ipv4_mapped_loopback_does_not_sneak_through_as_ipv6(self):
        """``::ffff:127.0.0.1`` reaches loopback wearing an IPv6 shape."""
        reason = _reject_address(ipaddress.ip_address('::ffff:127.0.0.1'))
        self.assertIn('loopback', reason)

    def test_carrier_grade_nat_is_refused_though_it_is_not_private(self):
        """100.64/10 is neither private nor global. Fail closed."""
        self.assertTrue(_reject_address(ipaddress.ip_address('100.64.0.1')))

    def test_a_public_address_is_allowed(self):
        """The guard has to let real webhooks through."""
        self.assertEqual(_reject_address(ipaddress.ip_address('93.184.216.34')), '')


class TestUrlValidation(BaseCase):
    """URL shapes, before DNS even matters."""

    def test_plain_http_is_refused(self):
        with _resolving_to('93.184.216.34'), self.assertRaises(OutboundError):
            validate_url('http://hooks.example.com/x')

    def test_credentials_in_the_url_are_refused(self):
        """``https://real-host@127.0.0.1/`` reads as one host, dials another."""
        with _resolving_to('93.184.216.34'), self.assertRaises(OutboundError):
            validate_url('https://hooks.example.com@127.0.0.1/x')

    def test_a_port_other_than_443_is_refused(self):
        """Every extra port widens what an internal scan can reach."""
        with _resolving_to('93.184.216.34'), self.assertRaises(OutboundError):
            validate_url('https://hooks.example.com:8069/x')

    def test_a_hostname_resolving_to_loopback_is_refused(self):
        """The https check never looked at where the name points."""
        with _resolving_to('127.0.0.1'), self.assertRaises(OutboundError) as caught:
            validate_url('https://totally-public.example.com/x')
        self.assertIn('loopback', str(caught.exception))

    def test_a_mixed_answer_is_refused_whole(self):
        """One public and one private answer is not a half-pass.

        Which address the connection would have used is not ours to
        pick, and accepting the pair gives a rebinding host a second
        chance.
        """
        with _resolving_to('93.184.216.34', '127.0.0.1'), \
                self.assertRaises(OutboundError):
            validate_url('https://mixed.example.com/x')

    def test_a_name_that_does_not_resolve_is_refused(self):
        with patch(
            'odoo.addons.incubacloud.net.outbound.getaddrinfo',
            side_effect=socket.gaierror('nope'),
        ), self.assertRaises(OutboundError):
            validate_url('https://nowhere.example.com/x')

    def test_a_public_https_url_passes(self):
        with _resolving_to('93.184.216.34'):
            parts = validate_url('https://hooks.example.com/incoming')
        self.assertEqual(parts.hostname, 'hooks.example.com')


class TestPinningAndDelivery(BaseCase):
    """The socket must go where validation said, not where DNS says next."""

    def test_the_connection_dials_the_validated_address(self):
        """Closes the TOCTOU: validate, then resolve again, is rebinding."""
        with patch(
            'odoo.addons.incubacloud.net.outbound.create_connection',
        ) as create:
            conn = _PinnedHTTPSConnection(
                'hooks.example.com', '93.184.216.34', port=443,
            )
            # Called the way ``HTTPConnection.connect`` calls it, through
            # the attribute — not the class method, which the stdlib
            # shadows on the instance.
            conn._create_connection(('hooks.example.com', 443), 10, None)
        self.assertEqual(create.call_args[0][0], ('93.184.216.34', 443))

    def test_the_hostname_survives_for_sni_and_certificate_checks(self):
        """Pinning must not turn into "trust any certificate"."""
        conn = _PinnedHTTPSConnection(
            'hooks.example.com', '93.184.216.34', port=443,
        )
        self.assertEqual(conn.host, 'hooks.example.com')

    def test_a_refused_destination_never_opens_a_socket(self):
        with _resolving_to('169.254.169.254'), patch(
            'odoo.addons.incubacloud.net.outbound._PinnedHTTPSConnection',
        ) as conn, self.assertRaises(OutboundError):
            post_json('https://metadata.example.com/x', b'{}')
        conn.assert_not_called()

    def test_a_delivery_reports_nothing_about_the_far_side(self):
        """Blind by design: no body, no status, nothing to probe with."""
        fake = MagicMock(spec=_PinnedHTTPSConnection)
        with _resolving_to('93.184.216.34'), patch(
            'odoo.addons.incubacloud.net.outbound._PinnedHTTPSConnection',
            return_value=fake,
        ):
            result = post_json('https://hooks.example.com/x', b'{"a": 1}')
        self.assertIsNone(result)
        fake.getresponse.return_value.read.assert_called_once()
        fake.close.assert_called_once()

    def test_the_body_read_is_bounded(self):
        """A hostile receiver must not hold the worker on an endless stream."""
        fake = MagicMock(spec=_PinnedHTTPSConnection)
        with _resolving_to('93.184.216.34'), patch(
            'odoo.addons.incubacloud.net.outbound._PinnedHTTPSConnection',
            return_value=fake,
        ):
            post_json('https://hooks.example.com/x', b'{}')
        self.assertTrue(fake.getresponse.return_value.read.call_args[0][0] > 0)


class TestNoRedirectsOutOfTheCheckedDestination(BaseCase):
    """A 302 used to be enough to leave the validated host entirely."""

    def test_the_shared_opener_refuses_redirects(self):
        from odoo.addons.incubacloud.github.http_utils import _NoRedirectHandler
        handler = _NoRedirectHandler()
        with self.assertRaises(Exception):
            handler.redirect_request(
                MagicMock(spec=['full_url']), None, 302, 'Found', {},
                'http://169.254.169.254/latest/meta-data/',
            )

    def test_no_notification_sender_opens_a_bare_url(self):
        """Structural: the guard is worth nothing if the next edit skips it.

        ``urlopen`` here would silently restore redirect-following and
        drop every address check, and it would look ordinary in review.
        """
        import inspect

        from odoo.addons.incubacloud.models import cloud_alert, cloud_job
        for module in (cloud_alert, cloud_job):
            source = inspect.getsource(module)
            self.assertNotIn(
                'urllib.request.urlopen', source,
                f'{module.__name__} reaches the network without the guard',
            )


@tagged('-at_install', 'post_install')
class TestPreferencesRoute(HttpCase):
    """Storing the URL is itself part of the sink.

    Driven over real HTTP so routing, session and controller all take
    part — calling the method directly would prove nothing about who can
    reach it, which is the whole question here.
    """

    def _make_user(self, login, groups):
        """Create a user with a known password for ``authenticate``.

        :param login: login and password (same string, test-only).
        :param groups: xml ids of the groups to grant.
        :returns: the created ``res.users`` record.
        """
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'password': login,
            'group_ids': [(6, 0, [self.env.ref(g).id for g in groups])],
        })

    def _save(self, **params):
        """POST to /cloud/save_user_preferences on the current session.

        :param params: JSON-RPC params for the route.
        :returns: the decoded ``result`` payload.
        """
        payload = {
            'jsonrpc': '2.0', 'method': 'call', 'id': 1,
            'params': {
                'cloud_notification_level': 'failures',
                'cloud_notification_mode': 'immediate',
            } | params,
        }
        return self.url_open(
            '/cloud/save_user_preferences',
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
        ).json().get('result')

    def test_a_portal_user_cannot_store_a_webhook_url(self):
        """Inert today only because both senders filter share=False.

        The SPA that owns this form is internal-only, so nothing
        legitimate is refused. What this closes is the direct RPC — and
        with it a trap that any future widening of the notification
        audience would spring on real customers.
        """
        portal = self._make_user('portal-webhook', ['base.group_portal'])
        self.authenticate('portal-webhook', 'portal-webhook')
        # A URL that passes every other check, so the account gate is
        # the only thing that can refuse it. Without this the test
        # passed on a DNS failure and said nothing about the gate —
        # caught by mutation.
        with _resolving_to('93.184.216.34'):
            result = self._save(cloud_webhook_url='https://hooks.example.com/x')
        self.assertFalse(result.get('ok'))
        self.assertIn('Not available', result.get('error', ''))
        self.assertFalse(portal.cloud_webhook_url)

    def test_an_internal_user_is_told_why_a_url_is_rejected(self):
        """A refusal the user can act on, not a silent drop."""
        self._make_user('internal-webhook', ['base.group_user'])
        self.authenticate('internal-webhook', 'internal-webhook')
        with _resolving_to('127.0.0.1'):
            result = self._save(
                cloud_webhook_url='https://looks-fine.example.com/x',
            )
        self.assertFalse(result.get('ok'))
        self.assertIn('loopback', result.get('error', ''))

    def test_an_internal_user_can_still_save_a_public_url(self):
        """The guard must not break the feature it protects."""
        user = self._make_user('internal-ok', ['base.group_user'])
        self.authenticate('internal-ok', 'internal-ok')
        with _resolving_to('93.184.216.34'):
            result = self._save(
                cloud_webhook_url='https://hooks.example.com/x',
            )
        self.assertTrue(result.get('ok'), result)
        self.assertEqual(user.cloud_webhook_url, 'https://hooks.example.com/x')
