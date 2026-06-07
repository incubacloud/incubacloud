"""
Tier 2 — controller test for the backup-backend connection test endpoint.

Covers the 301/PermanentRedirect handling: a bucket that exists but is
served from a different endpoint/region/jurisdiction must be reported as
an error (duplicity does not follow the redirect), not a false green.
"""
import boto3
from botocore.exceptions import ClientError
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_backends

# A real (offline-constructed) S3 client class to spec mocks against, so a
# call outside the boto3 client API surfaces as AttributeError in the test.
_S3_CLIENT_CLS = type(boto3.client(
    's3', region_name='us-east-1',
    aws_access_key_id='x', aws_secret_access_key='y',
))


class TestBackupBackendConnTest(TransactionCase):

    def setUp(self):
        super().setUp()
        self.backend = self.env['cloud.backup.backend'].create({
            'name': 'R2 EU',
            's3_bucket': 'mybucket',
            's3_access_key_id': 'AKIAEXAMPLE',
            's3_secret_access_key': 'secret',
            's3_endpoint_url': 'https://eu.example.r2.cloudflarestorage.com',
        })
        self.controller = _routes_backends.BackendsMixin()

    def _call_with(self, client_side_effect):
        """Invoke the endpoint with a mocked request env and boto3 client.

        *client_side_effect* is attached to the S3 client's head_bucket;
        pass an exception to raise it, or None for a healthy call.
        """
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        fake_client = MagicMock(spec=_S3_CLIENT_CLS)
        if client_side_effect is not None:
            fake_client.head_bucket.side_effect = client_side_effect
        with patch.object(_routes_backends, 'request', fake_req), \
                patch.object(
                    _routes_backends.boto3, 'client',
                    return_value=fake_client,
                ):
            return self.controller.cloud_test_backup_backend(self.backend.id)

    @staticmethod
    def _client_error(code):
        return ClientError(
            {'Error': {'Code': code, 'Message': 'Moved'}}, 'HeadBucket',
        )

    def test_301_reported_as_error(self):
        result = self._call_with(self._client_error('301'))
        self.assertFalse(result['ok'])
        self.assertIn('301', result['error'])

    def test_permanent_redirect_reported_as_error(self):
        result = self._call_with(self._client_error('PermanentRedirect'))
        self.assertFalse(result['ok'])
        self.assertIn('301', result['error'])

    def test_healthy_connection_ok(self):
        result = self._call_with(None)
        self.assertTrue(result['ok'])
