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
    _owner_field = 'instance_id'

    instance_id = fields.Many2one(
        comodel_name='cloud.instance',
        string='Instance',
        required=True,
        ondelete='cascade',
    )
    requirements_read_at = fields.Datetime(
        copy=False, readonly=True,
        help='Last time the panel read this line\'s requirements.txt, '
        'whether it existed or not. A deploy that cannot reach the Git '
        'server goes on with what it read then, and fails when it never '
        'read them.',
    )

    def _repo_owner(self):
        """The instance receives this line's pip/apt dependencies."""
        self.ensure_one()
        return self.instance_id
