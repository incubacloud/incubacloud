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
