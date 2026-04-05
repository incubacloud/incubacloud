from odoo import api, fields, models

from .encrypted_char import EncryptedChar
from .password_utils import generate_password


class CloudBackupBackend(models.Model):
    _name = 'cloud.backup.backend'
    _inherit = ['cloud.security.mixin']
    _description = 'Backup Backend'

    name = fields.Char(required=True)
    backend_type = fields.Selection(
        selection=[('s3', 'S3 Compatible')],
        string='Type',
        required=True,
        default='s3',
    )

    # ── S3 / compatible storage ─────────────────────────────────────────────

    s3_bucket = fields.Char(string='Bucket')
    s3_path = fields.Char(
        string='Path',
        default='backups',
        help='Object prefix inside the bucket (e.g. backups or clients/acme).',
    )
    s3_endpoint_url = fields.Char(
        string='Endpoint URL',
        help='Leave empty for AWS S3. Set for MinIO, Wasabi, Backblaze, etc.',
    )
    s3_access_key_id = fields.Char(string='Access Key ID')
    s3_secret_access_key = EncryptedChar(string='Secret Access Key')

    # ── Encryption ─────────────────────────────────────────────────────────

    passphrase = EncryptedChar(
        string='Encryption Passphrase',
        help='Auto-generated if left blank. Used as PASSPHRASE inside the backup container.',
    )

    # ── Docker image ────────────────────────────────────────────────────────

    backup_image_version = fields.Char(
        string='Image Version',
        default='latest',
        help='Tag of the docker-duplicity image (e.g. latest, 4.0.0).',
    )

    # ── Notifications ───────────────────────────────────────────────────────

    email_from = fields.Char(string='Notification From')
    email_to = fields.Char(string='Notification To')
    smtp_report_success = fields.Boolean(
        string='Report on Success',
        default=True,
    )

    # ── Retention ─────────────────────────────────────────────────────────────

    backup_retention = fields.Char(
        string='Retention Period',
        default='3M',
        help='How long to keep old backups (e.g. 3M = 3 months, 30D = 30 days, 1Y = 1 year).',
    )

    # ── Advanced ────────────────────────────────────────────────────────────

    deletion_via_cron = fields.Boolean(
        string='Enable Deletion Cron',
        default=False,
        help='Enables the daily backup deletion cron job inside the container.',
    )
    backup_tz = fields.Char(
        string='Timezone',
        default='UTC',
    )

    # ── Computed destination URL ────────────────────────────────────────────

    backup_dst = fields.Char(
        string='Backup Destination',
        compute='_compute_backup_dst',
        store=True,
        help='Computed duplicity DST URL (e.g. boto3+s3://bucket/path).',
    )

    @api.depends('backend_type', 's3_bucket', 's3_path')
    def _compute_backup_dst(self):
        for rec in self:
            if rec.backend_type == 's3' and rec.s3_bucket:
                path = (rec.s3_path or '').strip('/')
                if path:
                    rec.backup_dst = f"boto3+s3://{rec.s3_bucket}/{path}"
                else:
                    rec.backup_dst = f"boto3+s3://{rec.s3_bucket}"
            else:
                rec.backup_dst = ''

    # ── Password handling ───────────────────────────────────────────────────

    _PASSWORD_FIELDS = ('s3_secret_access_key', 'passphrase')

    def write(self, vals):
        self._check_can_manage_settings()
        for field in self._PASSWORD_FIELDS:
            if field in vals and not vals[field]:
                del vals[field]
        return super().write(vals)

    def unlink(self):
        self._check_can_manage_settings()
        return super().unlink()

    @api.model_create_multi
    def create(self, vals_list):
        self._check_can_manage_settings()
        for vals in vals_list:
            if not vals.get('passphrase'):
                vals['passphrase'] = generate_password()
        return super().create(vals_list)
