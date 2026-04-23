from odoo import api, fields, models
from odoo.exceptions import ValidationError


class CloudAlert(models.Model):
    _name = 'cloud.alert'
    _description = 'Cloud Infrastructure Alert'
    _order = 'create_date desc'

    host_id = fields.Many2one(
        'cloud.host',
        ondelete='cascade',
        string='Host',
        index=True,
    )
    instance_id = fields.Many2one(
        'cloud.instance',
        ondelete='cascade',
        string='Instance',
        index=True,
    )
    project_id = fields.Many2one(
        'cloud.project',
        ondelete='cascade',
        string='Project',
        index=True,
    )
    conflict_data = fields.Json(
        string='Conflict Data',
        help='List of pip dependency conflicts: [{name, existing, incoming}]',
    )
    job_id = fields.Many2one(
        'cloud.job',
        ondelete='set null',
        string='Originating Job',
        index=True,
    )
    code = fields.Char(
        required=True,
        index=True,
        help='Machine-readable identifier for deduplication (e.g. disk_critical).',
    )
    message = fields.Char(required=True)
    level = fields.Selection(
        [('warning', 'Warning'), ('critical', 'Critical')],
        default='warning',
        required=True,
    )
    state = fields.Selection(
        [('active', 'Active'), ('dismissed', 'Dismissed')],
        default='active',
        required=True,
    )
    create_date = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._check_target()
        records._broadcast_overview()
        return records

    def write(self, vals):
        res = super().write(vals)
        # Only broadcast on state transitions — field-level edits
        # (message tweaks, metadata) do not change the overview badge.
        if 'state' in vals:
            self._broadcast_overview()
        return res

    def unlink(self):
        # Capture the env before the rows go away so the broadcast
        # can still hit all active internal users.
        env = self.env
        res = super().unlink()
        self.browse()._broadcast_overview_from(env)
        return res

    @api.constrains('host_id', 'instance_id', 'project_id')
    def _check_target(self):
        for rec in self:
            if not rec.host_id and not rec.instance_id and not rec.project_id:
                raise ValidationError(
                    "An alert must be linked to a host, an instance, or a project."
                )

    def _broadcast_overview(self):
        """Notify every active internal user that the alert overview may
        have changed — they will refetch ``/cloud/get_alert_count`` and
        ``/cloud/get_dashboard`` through the normal ACL path.

        The payload is intentionally empty: the bus does not respect
        ``ir.rule`` filters, so we never ship actual alert data through
        it. The client treats the event as an invalidation tick only.
        """
        self._broadcast_overview_from(self.env)

    @api.model
    def _broadcast_overview_from(self, env):
        users = env['res.users'].search([
            ('share', '=', False),
            ('active', '=', True),
        ])
        for user in users:
            user._bus_send('cloud_overview', {})
