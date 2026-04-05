import uuid

from odoo import fields, models


class CloudConnectToken(models.TransientModel):
    _name = 'cloud.connect.token'
    _description = 'One-time session token for instance connect'
    _transient_max_hours = 0.02  # vacuum after ~1 minute

    token = fields.Char(
        required=True,
        index=True,
        default=lambda self: uuid.uuid4().hex,
    )
    instance_id = fields.Many2one('cloud.instance', required=True, ondelete='cascade')
    sid = fields.Char(required=True)
    instance_domain = fields.Char()
    wildcard_domain = fields.Char()
