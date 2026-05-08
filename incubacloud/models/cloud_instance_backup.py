from odoo import fields, models


class CloudInstanceBackup(models.Model):
    _name = 'cloud.instance.backup'
    _description = 'Instance Backup'
    _order = 'backup_time desc'

    instance_id = fields.Many2one(
        'cloud.instance', required=True, ondelete='cascade', index=True,
    )
    backup_type = fields.Selection(
        [('Full', 'Full'), ('Incremental', 'Incremental')],
        required=True,
    )
    backup_time = fields.Datetime(required=True, index=True)
    volumes = fields.Integer(default=1)
    chain_start = fields.Datetime()
    is_primary = fields.Boolean(default=False)
    attachment_id = fields.Many2one(
        'ir.attachment', string='Backup File',
        ondelete='set null', index=True,
        help='For non-production backups: the ZIP attachment (2h TTL).',
    )
    with_filestore = fields.Boolean(
        default=True,
        help=(
            'Non-production: whether the ZIP includes the filestore. '
            'Production rows ignore this — duplicity always backs up '
            'database + filestore as a chain.'
        ),
    )
    size = fields.Integer(
        string='Size (bytes)',
        help=(
            'Non-production: the attached ZIP size, captured at create '
            'time. Production: total bytes of the duplicity files for '
            'this backup set, populated by the backup_list job via '
            'boto3 listing of the S3 backend.'
        ),
    )
