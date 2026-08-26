"""SEC-012 — what an unsigned request is allowed to cost us.

The endpoint is public, POST and CSRF-exempt because GitHub cannot
authenticate any other way: trust comes from an HMAC over the body. That
has a consequence with no way around it — deciding a signature is false
*requires* hashing the whole body, so a flood of forged signatures will
always cost something. What can be controlled is how much, and that is
the product of two bounds: how large one request may be, and how many
are served. Both are asserted here.

The audit recorded this as "unlimited body", which is wrong: Odoo caps
every request at 128 MiB and werkzeug enforces it from Content-Length.
The real defect was the size of that headroom (GitHub itself never
sends more than 25 MB) and the order of the checks — the body was read
before anything decided from the headers alone had been looked at, so a
request missing a mandatory header still paid for a full read first.

Not covered here, deliberately: an edge IP allow-list restricted to
GitHub's published hook ranges. That is the only measure that removes
the unauthenticated exposure rather than bounding it, and it carries an
operational duty (those ranges change; missing one silently stops every
webhook) that has to be designed, not slipped in.
"""
from unittest.mock import MagicMock, patch

from odoo.http import HTTPRequest, Request, Response
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers import _rate_limit as rl
from odoo.addons.incubacloud.controllers import github_webhook as gw
from odoo.addons.incubacloud.github.webhook_utils import validate_hmac_sha256

_VALID_SIG = 'sha256=' + '0' * 64


class TestWebhookRequestCost(TransactionCase):

    def _call(self, headers, body=b'{}'):
        """Invoke the endpoint with a stubbed request.

        :param headers: request headers as a plain dict.
        :param body: what ``get_data`` returns if it is ever called.
        :return: tuple of the response and the ``get_data`` mock, so a
            test can assert the body was never read.
        """
        fake = MagicMock(spec=Request)
        fake.env = self.env
        fake.httprequest = MagicMock(spec=HTTPRequest)
        fake.httprequest.remote_addr = '198.51.100.7'
        fake.httprequest.headers = headers
        fake.httprequest.get_data.return_value = body
        # A real Response, not a mock: ``@http.route`` validates what
        # the endpoint returns and rejects anything else, so a mock here
        # fails the call for the wrong reason.
        fake.make_response.side_effect = (
            lambda body_, status=200, headers=None: Response(
                body_, status=status, headers=headers,
            )
        )
        # Two patches, not one: ``_rate_limit`` imports ``request``
        # into its own module namespace, so patching only the
        # controller's leaves the gate reaching for the real (unbound)
        # proxy.
        with patch.object(gw, 'request', fake), \
                patch.object(rl, 'request', fake):
            resp = gw.GitHubWebhookController().github_webhook()
        return resp, fake.httprequest.get_data

    # ── the size bound ─────────────────────────────────────────────

    def test_the_route_caps_the_body_below_odoo_s_default(self):
        """128 MiB of headroom for a 25 MB protocol is headroom the
        attacker spends, not us."""
        routing = gw.GitHubWebhookController.github_webhook.original_routing
        cap = routing.get('max_content_length')
        self.assertEqual(cap, gw._MAX_PAYLOAD_BYTES)
        self.assertLess(cap, 128 * 1024 * 1024)

    def test_the_cap_still_clears_the_largest_legitimate_delivery(self):
        """GitHub's own limit is 25 MB; the cap must sit above it or we
        would drop real pushes from a big monorepo."""
        self.assertGreater(gw._MAX_PAYLOAD_BYTES, 25 * 1000 * 1000)

    def test_the_cap_is_declared_per_route_not_globally(self):
        """The restore upload next door declares 2 GiB and must keep
        it — a global cap would have broken file-upload restores."""
        from odoo.addons.incubacloud.controllers.main import CloudController
        restore = CloudController.restore_instance_upload.original_routing
        self.assertEqual(
            restore.get('max_content_length'), 2 * 1024 * 1024 * 1024,
        )

    # ── the order of the checks ────────────────────────────────────

    def test_a_missing_delivery_header_is_refused_without_reading(self):
        resp, get_data = self._call({
            'X-GitHub-Event': 'push',
            'X-Hub-Signature-256': _VALID_SIG,
        })
        self.assertEqual(resp.status_code, 400)
        get_data.assert_not_called()

    def test_a_malformed_signature_is_refused_without_reading(self):
        """A signature that cannot validate against *any* secret is not
        worth hashing a body to disprove."""
        for bad in ('', 'garbage', 'sha256=', 'sha1=' + '0' * 40,
                    'sha256=' + 'z' * 64, 'sha256=' + '0' * 63):
            with self.subTest(signature=bad):
                resp, get_data = self._call({
                    'X-GitHub-Event': 'push',
                    'X-GitHub-Delivery': 'd-1',
                    'X-Hub-Signature-256': bad,
                })
                self.assertEqual(resp.status_code, 401)
                get_data.assert_not_called()

    def test_a_well_shaped_signature_does_reach_the_body(self):
        """The shape check is a cheap filter, not the trust boundary —
        anything shaped right still gets verified properly."""
        resp, get_data = self._call({
            'X-GitHub-Event': 'push',
            'X-GitHub-Delivery': 'd-2',
            'X-Hub-Signature-256': _VALID_SIG,
        })
        get_data.assert_called_once()
        # No GitHub App configured in this database, so it stops at 401
        # "not configured" — without having hashed anything.
        self.assertEqual(resp.status_code, 401)

    # ── the filter must not be stricter than the validator ─────────

    def test_the_shape_filter_accepts_every_signature_that_could_pass(self):
        """A filter tighter than the verifier would reject real
        deliveries. Ties the two together instead of trusting that the
        regex was written to match.
        """
        secret = 'a-webhook-secret'
        for body in (b'', b'{}', b'{"ref":"refs/heads/main"}', b'\xff\xfe'):
            with self.subTest(body=body[:12]):
                import hashlib
                import hmac
                sig = 'sha256=' + hmac.new(
                    secret.encode(), body, hashlib.sha256,
                ).hexdigest()
                self.assertTrue(
                    gw._SIGNATURE_RE.match(sig),
                    'the pre-filter would reject a genuine signature',
                )
                self.assertTrue(validate_hmac_sha256(body, sig, secret))
