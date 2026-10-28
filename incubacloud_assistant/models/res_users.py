"""Whether a user has read the assistant's privacy notice."""

from odoo import fields, models


class ResUsers(models.Model):
    """When the user accepted the assistant's first-use notice."""

    _inherit = "res.users"

    assistant_notice_at = fields.Datetime(
        string="Assistant notice accepted at",
        readonly=True,
        copy=False,
        groups="base.group_system",
    )
