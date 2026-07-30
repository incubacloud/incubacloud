"""Instance repository line.

Thin concrete model over ``cloud.repo.mixin`` — validation and the
requirements sync are shared with ``cloud.project.repo``; only the
owner FK differs.
"""
from odoo import fields, models


class CloudInstanceRepo(models.Model):
    _name = 'cloud.instance.repo'
    _inherit = 'cloud.repo.mixin'
    _description = 'Cloud Instance Repository'
    _order = 'sequence, id'

    instance_id = fields.Many2one(
        comodel_name='cloud.instance',
        string='Instance',
        required=True,
        ondelete='cascade',
    )

    def _repo_owner(self):
        """The instance receives this line's pip/apt dependencies."""
        self.ensure_one()
        return self.instance_id
