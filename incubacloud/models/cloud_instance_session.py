from odoo import fields, models


class CloudInstanceSession(models.Model):
    _name = 'cloud.instance.session'
    _description = 'Cloud Instance Terminal Session'
    _order = 'opened_at desc'
    _rec_name = 'instance_id'

    instance_id = fields.Many2one(
        'cloud.instance', string='Instance', required=True, ondelete='cascade',
    )
    service = fields.Char(string='Service', default='odoo')
    state = fields.Selection(
        [('open', 'Open'), ('closed', 'Closed')],
        default='open', required=True,
    )
    user_id = fields.Many2one(
        'res.users', string='User',
        default=lambda self: self.env.user, required=True,
    )
    opened_at = fields.Datetime(default=fields.Datetime.now, required=True)
    closed_at = fields.Datetime()
    session_id = fields.Char(
        string='Session ID', required=True, index=True, readonly=True,
    )
