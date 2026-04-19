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

    def test_health_ok_returns_200_and_json(self):
        """Happy path: DB responds + cloud module is loaded."""
        response = self.url_open('/cloud/health')
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
        self.assertEqual(payload['checks'].get('db'), 'ok')
        self.assertEqual(payload['checks'].get('registry'), 'ok')

    def test_health_is_public_no_auth_required(self):
        """The endpoint must work without a session cookie — that's the
        whole point of a liveness probe."""
        # Drop any session state the test harness may have set up.
        self.opener.cookies.clear()
        response = self.url_open('/cloud/health')
        self.assertEqual(response.status_code, 200)
