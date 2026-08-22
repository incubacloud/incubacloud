"""Tests for SEC-003 — cookie attributes and the HSTS retrofits.

Two independent halves, both of which have to hold for the hole to be
closed:

  * Odoo's ``set_cookie`` must stop emitting the session cookie without
    ``Secure`` and ``SameSite`` — but only when the request is actually
    secure, so a development instance over plain HTTP is untouched.
  * Traefik must actually send ``Strict-Transport-Security``. It does not
    today: the middleware that carries the header is defined on every host
    and referenced by no router, and the one that *is* referenced sets
    ``forceSTSHeader`` with ``stsSeconds`` at zero, which emits nothing.

The cookie tests drive the real ``FutureResponse`` and the real
``_Response`` — no hand-written stand-in — with the request mocked against
``odoo.http.Request`` so an attribute the real class does not have blows up
here instead of in production.
"""
from unittest.mock import MagicMock, patch

import werkzeug.wrappers
import yaml

from odoo.http import FutureResponse, Request, _Response
from odoo.tests.common import BaseCase, TransactionCase

from ..models.cloud_host import CloudHost, _read_traefik_template
from ..models.http_session_cookie import SAMESITE_LAX_COOKIES


def _mock_request(is_secure):
    """Build a request mock spec'd against the real ``odoo.http.Request``.

    ``db`` and ``httprequest`` are assigned in ``Request.__init__``, so the
    spec does not know them and they have to be set explicitly. ``db`` is
    left falsy to skip the ``ir.http._is_allowed_cookie`` lookup, which
    needs a registry.

    :param bool is_secure: what ``httprequest.is_secure`` should report.
    :returns: the configured mock.
    """
    request = MagicMock(spec=Request)
    request.db = False
    request.httprequest = MagicMock(spec=werkzeug.wrappers.Request)
    request.httprequest.is_secure = is_secure
    return request


def _cookie_header(response):
    """Return the single ``Set-Cookie`` value the response carries."""
    values = response.headers.getlist('Set-Cookie')
    assert len(values) == 1, values
    return values[0]


class TestSessionCookieAttributes(BaseCase):
    """The wrappers installed by ``http_session_cookie``."""

    def _future_cookie(self, key, is_secure, **kwargs):
        """Set *key* on a real ``FutureResponse`` and return the header."""
        with patch('odoo.http.request', _mock_request(is_secure)):
            response = FutureResponse()
            response.set_cookie(key, 'value', **kwargs)
        return _cookie_header(response)

    def test_session_cookie_over_https_is_secure_and_lax(self):
        header = self._future_cookie('session_id', True)
        self.assertIn('Secure', header)
        self.assertIn('SameSite=Lax', header)

    def test_session_cookie_over_http_gets_no_secure(self):
        """Development runs over plain HTTP and must keep working.

        A ``Secure`` cookie set on an insecure origin is dropped by the
        browser, which would lock the developer out of their own instance.
        """
        header = self._future_cookie('session_id', False)
        self.assertNotIn('Secure', header)
        self.assertIn('SameSite=Lax', header)

    def test_other_cookies_get_secure_but_not_samesite(self):
        """``Secure`` is free; ``SameSite`` is not.

        Cookies such as the livechat or website visitor ones may need
        cross-site delivery, so only the authentication cookie is pinned.
        """
        header = self._future_cookie('frontend_lang', True)
        self.assertIn('Secure', header)
        self.assertNotIn('SameSite', header)

    def test_explicit_samesite_is_not_overridden(self):
        header = self._future_cookie('session_id', True, samesite='Strict')
        self.assertIn('SameSite=Strict', header)
        self.assertNotIn('SameSite=Lax', header)

    def test_response_class_is_patched_too(self):
        """The expired-session handler sets the cookie on a ``_Response``.

        It is a different class from ``FutureResponse`` and a different
        code path, so it needs its own coverage — patching only one of the
        two would leave the cookie unhardened every time a session expires.
        """
        with patch('odoo.http.request', _mock_request(True)):
            response = _Response()
            response.set_cookie('session_id', 'value')
        header = _cookie_header(response)
        self.assertIn('Secure', header)
        self.assertIn('SameSite=Lax', header)

    def test_only_the_session_cookie_is_pinned(self):
        self.assertEqual(SAMESITE_LAX_COOKIES, frozenset({'session_id'}))


_SEED_CONFIG = """\
http:
  middlewares:
    compress:
      compress: "true"
    secure:
      headers:
        forceSTSHeader: "true"
        frameDeny: "true"
"""

_SEED_TRAEFIK = """\
global:
  sendAnonymousUsage: false

entryPoints:
  http:
    address: ":80"
  https:
    http:
      tls: "true"
    address: ":443"

api:
  dashboard: true
"""

#: A dynamic config that already defines the middleware, so the entrypoint
#: retrofit is allowed to reference it.
_WITH_HSTS = CloudHost._add_traefik_hsts_middleware(_SEED_CONFIG)


class TestTraefikHstsRetrofit(TransactionCase):
    """The two edits that make Traefik actually emit HSTS.

    They only work together — the middleware lives in the dynamic config
    and the reference to it in the static one — so both are pinned here.
    """

    def test_middleware_is_added(self):
        out = CloudHost._add_traefik_hsts_middleware(_SEED_CONFIG)
        parsed = yaml.safe_load(out)
        self.assertEqual(
            parsed['http']['middlewares']['hsts']['headers'],
            {'forceSTSHeader': 'true', 'stsSeconds': 31536000},
        )

    def test_middleware_leaves_the_rest_alone(self):
        out = CloudHost._add_traefik_hsts_middleware(_SEED_CONFIG)
        parsed = yaml.safe_load(out)
        del parsed['http']['middlewares']['hsts']
        self.assertEqual(parsed, yaml.safe_load(_SEED_CONFIG))

    def test_middleware_is_idempotent(self):
        once = CloudHost._add_traefik_hsts_middleware(_SEED_CONFIG)
        self.assertEqual(CloudHost._add_traefik_hsts_middleware(once), once)

    def test_entrypoint_reference_is_added(self):
        out = CloudHost._add_traefik_entrypoint_hsts(_SEED_TRAEFIK, _WITH_HSTS)
        parsed = yaml.safe_load(out)
        self.assertEqual(
            parsed['entryPoints']['https']['http']['middlewares'],
            ['hsts@file'],
        )
        # The entrypoint keeps everything it had.
        self.assertEqual(parsed['entryPoints']['https']['address'], ':443')
        self.assertEqual(parsed['entryPoints']['https']['http']['tls'], 'true')

    def test_entrypoint_reference_is_idempotent(self):
        once = CloudHost._add_traefik_entrypoint_hsts(_SEED_TRAEFIK, _WITH_HSTS)
        self.assertEqual(CloudHost._add_traefik_entrypoint_hsts(once, _WITH_HSTS), once)

    def test_entrypoint_respects_hand_written_middlewares(self):
        """An operator's own chain wins.

        Appending to it blind is how a retrofit turns a working proxy into
        one that will not start.
        """
        custom = _SEED_TRAEFIK.replace(
            '      tls: "true"\n',
            '      tls: "true"\n      middlewares:\n        - mine@file\n',
        )
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_hsts(custom, _WITH_HSTS), custom,
        )

    def test_entrypoint_reference_needs_the_middleware_to_exist(self):
        """The pair fails closed.

        If the middleware retrofit did not apply — a ``config.yml`` shaped
        in a way it refuses to guess at — the entrypoint must not gain a
        reference to something the provider will not define. Getting this
        backwards does not lose a header, it 500s the whole host.
        """
        without = 'http:\n  routers: {}\n'
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_hsts(_SEED_TRAEFIK, without),
            _SEED_TRAEFIK,
        )
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_hsts(_SEED_TRAEFIK, ''),
            _SEED_TRAEFIK,
        )

    def test_both_are_no_ops_on_unknown_shapes(self):
        for junk in ('', 'http:\n  routers: {}\n', 'nothing: here\n'):
            self.assertEqual(
                CloudHost._add_traefik_hsts_middleware(junk), junk,
            )
            self.assertEqual(
                CloudHost._add_traefik_entrypoint_hsts(junk, _WITH_HSTS), junk,
            )

    def test_shipped_templates_already_carry_it(self):
        """The seed files and the retrofit must not disagree.

        If they did, every freshly provisioned host would immediately be
        rewritten by the retrofit — or worse, keep a shape the retrofit
        does not recognise and never get the header.
        """
        config = _read_traefik_template('config.yml')
        traefik = _read_traefik_template('traefik.yml')
        self.assertEqual(
            CloudHost._add_traefik_hsts_middleware(config), config,
        )
        self.assertEqual(
            CloudHost._add_traefik_entrypoint_hsts(traefik, config), config and traefik,
        )
        parsed = yaml.safe_load(traefik)
        self.assertEqual(
            parsed['entryPoints']['https']['http']['middlewares'],
            ['hsts@file', 'ratelimit@file'],
        )

    def test_entrypoint_reference_resolves_to_a_defined_middleware(self):
        """The two files have to agree on the name.

        An entrypoint that names a middleware the file provider does not
        define is not a missing header — Traefik answers 500 on every
        router of that entrypoint. That is the whole host, so the link
        between the two templates is pinned rather than assumed.
        """
        traefik = yaml.safe_load(
            _read_traefik_template('traefik.yml'),
        )
        config = yaml.safe_load(_read_traefik_template('config.yml'))
        referenced = traefik['entryPoints']['https']['http']['middlewares']
        defined = config['http']['middlewares']
        for name in referenced:
            self.assertTrue(
                name.endswith('@file'),
                f"{name} is not served by the file provider",
            )
            self.assertIn(name[:-len('@file')], defined)

    def test_secure_middleware_is_not_the_entrypoint_default(self):
        """``secure`` must stay off the entrypoint.

        It sets ``frameDeny``, which blanks out the panel's Grafana
        iframes and breaks Odoo's website editor, and it sets
        ``stsIncludeSubdomains``/``stsPreload``, which would make an
        expired tenant certificate unreachable with no click-through.
        Those are HARD-001 decisions, not this one's.
        """
        traefik = _read_traefik_template('traefik.yml')
        middlewares = yaml.safe_load(traefik)['entryPoints']['https']['http']
        self.assertNotIn('secure@file', middlewares['middlewares'])
        self.assertNotIn('doodba@file', middlewares['middlewares'])
