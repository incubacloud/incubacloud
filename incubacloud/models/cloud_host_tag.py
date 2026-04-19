from odoo import fields, models


class CloudHostTag(models.Model):
    _name = 'cloud.host.tag'
    _description = 'Cloud Host Tag'
    _order = 'name'

    name = fields.Char(required=True, translate=True)
    color = fields.Integer(
        string='Color Index',
        default=0,
        help='Color palette index (0–9) for the tag chip display.',
    )
