from odoo import fields, models

DEFAULT_WHITELIST = [
    'cdnjs.cloudflare.com',
    'fonts.googleapis.com',
    'fonts.gstatic.com',
    'www.google.com',
    'www.gravatar.com',
]


class CloudHostWhitelist(models.Model):
    _name = 'cloud.host.whitelist'
    _description = 'Host Whitelist Entry'
    _order = 'sequence, id'

    host_id = fields.Many2one(
        'cloud.host',
        required=True,
        ondelete='cascade',
        index=True,
    )
    hostname = fields.Char(
        required=True,
        help='Hostname allowed for test instances on this host '
             '(e.g. fonts.googleapis.com).',
    )
    sequence = fields.Integer(default=10)
