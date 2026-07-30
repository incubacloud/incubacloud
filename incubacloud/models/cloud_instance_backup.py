from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class CloudInstanceBackup(models.Model):
    _name = 'cloud.instance.backup'
    _description = 'Instance Backup'
    _order = 'backup_time desc'

    instance_id = fields.Many2one(
        'cloud.instance', required=True, ondelete='cascade', index=True,
    )
    kind = fields.Selection(
        [('chain', 'Chain'), ('archive', 'Archive')],
        required=True, default='chain', index=True,
        help=(
            'Internal split of the two backup semantics that used to share '
            'this model by convention only: "chain" rows mirror a duplicity '
            'backup set on the production backend (synced by backup_list, '
            'pruned when retention purges the set); "archive" rows are '
            'one-shot non-production ZIP dumps with a short-lived download '
            'attachment. The UI keeps calling both "Backups" on purpose — '
            'this field exists for the code, not for the user.'
        ),
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

    @api.constrains('kind', 'attachment_id', 'chain_start')
    def _check_kind_artifacts(self):
        """Refuse rows mixing the two semantics.

        A chain row's artifact lives on the duplicity backend, never as
        an attachment; an archive row is a standalone ZIP and can never
        belong to a duplicity chain. (An archive's attachment MAY be
        empty — the download TTL clears it — so only the cross-wiring is
        forbidden, not the absence.)
        """
        for rec in self:
            if rec.kind == 'chain' and rec.attachment_id:
                raise ValidationError(_(
                    'A chain backup cannot carry a download attachment; '
                    'that is an archive artifact.'
                ))
            if rec.kind == 'archive' and rec.chain_start:
                raise ValidationError(_(
                    'An archive backup cannot belong to a duplicity '
                    'chain.'
                ))
