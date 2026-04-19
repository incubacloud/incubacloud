"""Backup backend CRUD and connectivity test.

Mixed into ``CloudDataLoadController`` in ``data_load.py``.
"""
import logging

import boto3
from botocore.exceptions import ClientError, ParamValidationError

from odoo import _, http
from odoo.http import request

from ._helpers import _capped_search

_logger = logging.getLogger(__name__)


class BackendsMixin:
    """Backup backend CRUD."""

    # ── Backup backend endpoints ──────────────────────────────────────────────

    _BACKUP_BACKEND_ALLOWED = {
        'name', 'backend_type',
        's3_bucket', 's3_path', 's3_endpoint_url', 's3_access_key_id',
        's3_secret_access_key',
        'passphrase',
        'backup_image_version',
        'email_from', 'email_to', 'smtp_report_success',
        'backup_retention',
        'deletion_via_cron', 'backup_tz',
    }

    @http.route(['/cloud/get_backup_backends'], type='jsonrpc', auth='user')
    def cloud_get_backup_backends(self):
        backends, total, truncated, limit = _capped_search(
            request.env['cloud.backup.backend'], order='name',
        )
        # Count instances per backend. We cap the instance scan too to
        # protect the dashboard when the system has tens of thousands of
        # instances — usage counts become approximate above the cap, but
        # the alternative is blowing up the worker.
        instances, _itotal, _itrunc, _ilim = _capped_search(
            request.env['cloud.instance'],
        )
        usage = {}
        for inst in instances:
            bid = inst.effective_backup_backend.id
            if bid:
                usage[bid] = usage.get(bid, 0) + 1
        return {
            'items': [
                {
                    'id': b.id,
                    'name': b.name,
                    'backend_type': b.backend_type,
                    's3_bucket': b.s3_bucket or '',
                    's3_path': b.s3_path or '',
                    's3_endpoint_url': b.s3_endpoint_url or '',
                    'backup_dst': b.backup_dst or '',
                    'instance_count': usage.get(b.id, 0),
                }
                for b in backends
            ],
            'total': total,
            'truncated': truncated,
            'limit': limit,
        }

    @http.route(['/cloud/get_backup_backend'], type='jsonrpc', auth='user')
    def cloud_get_backup_backend(self, backend_id):
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found')}
        return {
            'id': b.id,
            'name': b.name,
            'backend_type': b.backend_type,
            's3_bucket': b.s3_bucket or '',
            's3_path': b.s3_path or '',
            's3_endpoint_url': b.s3_endpoint_url or '',
            's3_access_key_id': b.s3_access_key_id or '',
            'has_s3_secret_access_key': bool(b.s3_secret_access_key),
            'has_passphrase': bool(b.passphrase),
            'backup_image_version': b.backup_image_version or 'latest',
            'email_from': b.email_from or '',
            'email_to': b.email_to or '',
            'smtp_report_success': b.smtp_report_success,
            'backup_retention': b.backup_retention or '3M',
            'deletion_via_cron': b.deletion_via_cron,
            'backup_tz': b.backup_tz or 'UTC',
            'backup_dst': b.backup_dst or '',
        }

    @http.route(['/cloud/create_backup_backend'], type='jsonrpc', auth='user')
    def cloud_create_backup_backend(self, vals):
        self._sec()._check_can_manage_settings()
        safe = {
            k: v for k, v in vals.items()
            if k in self._BACKUP_BACKEND_ALLOWED
        }
        b = request.env['cloud.backup.backend'].create(safe)
        return {'id': b.id, 'name': b.name}

    @http.route(['/cloud/save_backup_backend'], type='jsonrpc', auth='user')
    def cloud_save_backup_backend(self, backend_id, vals):
        self._sec()._check_can_manage_settings()
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found')}
        safe = {
            k: v for k, v in vals.items()
            if k in self._BACKUP_BACKEND_ALLOWED
        }
        b.write(safe)
        return {'ok': True}

    @http.route(['/cloud/delete_backup_backend'], type='jsonrpc', auth='user')
    def cloud_delete_backup_backend(self, backend_id):
        self._sec()._check_can_manage_settings()
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found')}
        instances = request.env['cloud.instance'].search([
            ('backup_backend_id', '=', backend_id),
        ])
        if instances:
            names = ', '.join(instances.mapped('name'))
            return {
                'ok': False,
                'error': _(
                    'Cannot delete: this backend is used by %d '
                    'instance(s): %s. Remove the backend assignment '
                    'from those instances first.',
                    len(instances), names,
                ),
            }
        b.unlink()
        return {'ok': True}

    @http.route(['/cloud/test_backup_backend'], type='jsonrpc', auth='user')
    def cloud_test_backup_backend(self, backend_id):
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found.')}
        if not b.s3_bucket:
            return {'ok': False, 'error': _('No bucket configured.')}

        kwargs = {
            'aws_access_key_id':     b.s3_access_key_id or None,
            'aws_secret_access_key': b.s3_secret_access_key or None,
        }
        if b.s3_endpoint_url:
            kwargs['endpoint_url'] = b.s3_endpoint_url

        try:
            client = boto3.client('s3', **kwargs)
            client.head_bucket(Bucket=b.s3_bucket)
            return {'ok': True}
        except ParamValidationError as e:
            return {'ok': False, 'error': _('Invalid bucket name "%s": '
                    'bucket names cannot contain slashes. '
                    'Put only the bucket name here and the prefix in the Path field.') % b.s3_bucket}
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            msg = e.response.get('Error', {}).get('Message', str(e))
            if code in ('301', 'PermanentRedirect'):
                # Bucket exists but in a different region — still reachable
                return {'ok': True}
            if code == '403':
                return {
                    'ok': False,
                    'error': _('Access denied (403): check your credentials and bucket policy.'),
                }
            if code == '404':
                return {'ok': False, 'error': _('Bucket "%s" not found (404).') % b.s3_bucket}
            return {'ok': False, 'error': _('S3 error %(code)s: %(msg)s') % {'code': code, 'msg': msg}}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
