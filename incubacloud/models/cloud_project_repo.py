"""Project repository line.

Thin concrete model over ``cloud.repo.mixin`` — validation and the
requirements sync are shared with ``cloud.instance.repo``; only the
owner FK differs.
"""
from odoo import fields, models


class CloudProjectRepo(models.Model):
    _name = 'cloud.project.repo'
    _inherit = 'cloud.repo.mixin'
    _description = 'Cloud Project Repository'
    _order = 'sequence, id'

    project_id = fields.Many2one(
        comodel_name='cloud.project',
        string='Project',
        required=True,
        ondelete='cascade',
    )

    def _repo_owner(self):
        """The project receives this line's pip/apt dependencies."""
        self.ensure_one()
        return self.project_id
