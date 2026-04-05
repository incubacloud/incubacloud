from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    cloud_notification_level = fields.Selection(
        selection=[
            ('all', 'All events (success + failure)'),
            ('failures', 'Failures only'),
            ('none', 'None'),
        ],
        string='Cloud Job Notifications',
        default='failures',
    )
