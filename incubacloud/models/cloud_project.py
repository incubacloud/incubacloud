import re

from odoo import api, fields, models, _
from odoo.fields import Domain
from odoo.tools import SQL, Query
from odoo.exceptions import UserError


def _slugify(name):
    """Convert a name to a safe remote folder identifier (lowercase, hyphens)."""
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').lower().strip())
    return slug.strip('-') or 'project'


class CloudProject(models.Model):
    _name = 'cloud.project'
    _inherit = ['cloud.security.mixin']
    _description = 'Cloud Project'

    name = fields.Char(
        required=True,
        translate=True,
    )
    remote_folder = fields.Char(
        string='Remote Folder',
        compute='_compute_remote_folder',
        store=True,
        readonly=False,
        help='Folder name used on remote hosts: ~/remote_folder/instance_name/. '
             'Auto-generated from the project name; can be overridden.',
    )
    description = fields.Text(
        string='Description',
        translate=True,
    )
    tag_ids = fields.Many2many(
        comodel_name='cloud.tag',
        relation='cloud_project_tag_rel',
        column1='project_id',
        column2='tag_id',
        string='Tags',
    )
    member_ids = fields.Many2many(
        comodel_name='res.users',
        relation='cloud_project_member_rel',
        column1='project_id',
        column2='user_id',
        string='Members',
        help='Users assigned to this project. Stakeholders and Consultants '
             'can only see projects they are members of.',
    )
    instance_ids = fields.One2many(
        comodel_name='cloud.instance',
        inverse_name='project_id',
        string='Instances',
    )
    repo_ids = fields.One2many(
        comodel_name='cloud.project.repo',
        inverse_name='project_id',
        string='Repositories',
    )
    project_author = fields.Char(
        string='Project Author',
        default='IncubaCloud',
    )
    project_license = fields.Selection(
        selection=[
            ('no_license',      'No license'),
            ('Apache-2.0',      'Apache 2.0'),
            ('BSL-1.0',         'Boost Software License 1.0'),
            ('AGPL-3.0-or-later', 'GNU AGPL 3.0 or later'),
            ('LGPL-3.0-or-later', 'GNU LGPL 3.0 or later'),
            ('MIT',             'MIT'),
            ('OEEL-1.0',        'Odoo Enterprise Edition License v1.0'),
            ('OPL-1.0',         'Odoo Proprietary License v1.0'),
        ],
        string='License',
        default='BSL-1.0',
    )
    odoo_version = fields.Selection(
        selection=[
            ('7.0', '7.0'),   ('8.0', '8.0'),   ('9.0', '9.0'),
            ('10.0', '10.0'), ('11.0', '11.0'), ('12.0', '12.0'),
            ('13.0', '13.0'), ('14.0', '14.0'), ('15.0', '15.0'),
            ('16.0', '16.0'), ('17.0', '17.0'), ('18.0', '18.0'),
            ('19.0', '19.0'),
        ],
        string='Odoo Version',
        default='19.0',
        required=True,
    )

    odoo_commit_sha = fields.Char(
        string='Pinned Odoo Commit',
        help=(
            'When set, the Odoo core repository is frozen to this exact'
            ' commit SHA instead of following the branch tip.'
        ),
    )

    pip_dependencies = fields.Text(
        string='Python Dependencies',
        default=(
            'git+https://github.com/OCA/openupgradelib.git@master\n'
            'unicodecsv\n'
            'unidecode\n'
        ),
        help='Contents written to odoo/custom/dependencies/pip.txt on deploy.',
    )
    apt_dependencies = fields.Text(
        string='System (APT) Dependencies',
        help='Contents written to odoo/custom/dependencies/apt.txt on deploy.',
    )

    odoo_initial_lang = fields.Many2one(
        comodel_name='res.lang',
        string='Initial Language',
        help='Default language installed when deploying instances in this project.',
    )
    backup_backend_id = fields.Many2one(
        comodel_name='cloud.backup.backend',
        string='Backup Backend',
        help='Default backup backend for all production instances in this project.',
    )
    pr_reviews_enabled = fields.Boolean(
        string='Auto-create PR preview instances',
        default=True,
        help='When enabled, opening a PR on a repo configured in the '
             'production instance automatically creates a staging preview '
             'instance with the PR branch and a clone of prod data.',
    )

    status = fields.Selection(
        selection=[
            ('ok', 'OK'),
            ('warning', 'Warning'),
            ('error', 'Error'),
        ],
        string='Status',
        compute='_compute_status',
        store=True,
    )

    @api.depends('name')
    def _compute_remote_folder(self):
        for project in self:
            if not project.remote_folder:
                project.remote_folder = _slugify(project.name or '')

    # Fields tracked in the audit log when changed
    _AUDIT_TRACKED_FIELDS = frozenset({
        'name', 'odoo_version', 'backup_backend_id',
        'pr_reviews_enabled', 'member_ids',
    })

    @api.model_create_multi
    def create(self, vals_list):
        self._check_can_create_project()
        return super().create(vals_list)

    def unlink(self):
        self._check_can_delete_project()
        return super().unlink()

    def write(self, vals):
        # Snapshot tracked fields before write
        changed = self._AUDIT_TRACKED_FIELDS & set(vals)
        old_snap = {}
        if changed:
            old_snap = {f: {r.id: r[f] for r in self} for f in changed}
        result = super().write(vals)
        if changed:
            for rec in self:
                parts = []
                for field in changed:
                    old = old_snap[field][rec.id]
                    new = rec[field]
                    if old == new:
                        continue
                    field_def = rec._fields[field]
                    old_d = (
                        old.display_name
                        if hasattr(old, 'display_name') and old
                        else str(old)
                    )
                    new_d = (
                        new.display_name
                        if hasattr(new, 'display_name') and new
                        else str(new)
                    )
                    parts.append(f"{field_def.string}: {old_d}→{new_d}")
                if parts:
                    self.env['cloud.audit.log'].sudo().create({
                        'action': 'Config changed',
                        'project_id': rec.id,
                        'details': '; '.join(parts)[:255],
                    })
        return result

    @api.depends('instance_ids.status')
    def _compute_status(self):
        for project in self:
            if any(
                instance.status == 'error'
                for instance in project.instance_ids
            ):
                project.status = 'error'
            elif any(
                instance.status == 'warning'
                for instance in project.instance_ids
            ):
                project.status = 'warning'
            else:
                project.status = 'ok'
