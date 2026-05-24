"""HTTP-level tests for the public /cloud/health liveness probe.

Uses HttpCase so the request actually goes through the routing layer
(werkzeug → Odoo http dispatch → controller), which is what Docker
healthcheck and load balancers exercise in production.
"""
import json

from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged('-at_install', 'post_install')
class TestCloudHealth(HttpCase):
    """The probe targets a public endpoint that still needs a DB context
    (``auth='public'`` routes are registered per-DB in Odoo 18, not
    server-wide). We pass ``X-Odoo-Database`` explicitly to mimic what a
    real probe configures via the Host/DB-selector layer in front of
    Odoo (nginx/Traefik rule, or a cookie). Without the header the
    dispatcher returns 404 with ``"No database is selected"``.
    """

    def _probe(self):
        return self.url_open(
            '/cloud/health',
            headers={'X-Odoo-Database': self.env.cr.dbname},
        )

    def test_health_ok_returns_200_and_json(self):
        """Happy path: DB responds + cloud module is loaded.

        The body is intentionally minimal (just ``{"status": "ok"}``) to
        avoid fingerprinting the stack to unauthenticated probes — the
        controller comment in ``cloud_health`` documents that contract.
        """
        response = self._probe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get('Content-Type'),
            'application/json',
        )
        # Response must not be cached — Docker/LB need fresh state.
        self.assertIn(
            'no-store',
            (response.headers.get('Cache-Control') or '').lower(),
        )
        payload = json.loads(response.text)
        self.assertEqual(payload['status'], 'ok')

    def test_health_is_public_no_auth_required(self):
        """The endpoint must work without a session cookie — that's the
        whole point of a liveness probe."""
        # Drop any session state the test harness may have set up.
        self.opener.cookies.clear()
        response = self._probe()
        self.assertEqual(response.status_code, 200)
