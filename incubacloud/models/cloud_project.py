import re

from odoo import api, fields, models, _
from odoo.fields import Domain
from odoo.tools import SQL, Query
from odoo.exceptions import UserError, ValidationError

from ._odoo_versions import ODOO_VERSION_SELECTION


def _slugify(name):
    """Convert a name to a safe remote folder identifier (lowercase, hyphens)."""
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').lower().strip())
    return slug.strip('-') or 'project'


# remote_folder fluye a paths remotos (~/{folder}/{instance}) y a
# COMPOSE_PROJECT_NAME. Mismo regex que cloud.instance.name — lowercase
# alnum + [_-], 1-63 chars — para bloquear command injection si un user
# sobrescribe el valor computed.
_REMOTE_FOLDER_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}$')


class CloudProject(models.Model):
    _name = 'cloud.project'
    _inherit = ['cloud.security.mixin', 'cloud.audit.tracked.mixin']
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
        selection=ODOO_VERSION_SELECTION,
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
        string='Backup Destination',
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

    @api.constrains('remote_folder')
    def _check_remote_folder_shell_safe(self):
        for p in self:
            if p.remote_folder and not _REMOTE_FOLDER_RE.match(p.remote_folder):
                raise ValidationError(_(
                    "Remote folder '%(folder)s' is invalid. It must start "
                    "with a lowercase letter or digit and contain only "
                    "lowercase letters, digits, hyphens and underscores "
                    "(max 63 chars).",
                    folder=p.remote_folder,
                ))

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
        # A project can only be dropped once none of its instances still
        # live on a host: a deployed (or in-flight) instance must be
        # taken off its host first, or deleting the project would orphan
        # the remote stack. The instance-level unlink guard enforces the
        # same rule on the cascade; checking here gives a clearer message
        # and covers archived-but-deployed instances too.
        on_host = self.with_context(active_test=False).instance_ids.filtered(
            lambda i: i.state != 'draft'
        )
        if on_host:
            raise UserError(
                _(
                    "Cannot delete a project whose instance(s) are still on "
                    "a host: %(names)s. Remove them from their hosts first.",
                    names=", ".join(on_host.mapped("name")),
                )
            )
        return super().unlink()

    def _audit_target_vals(self):
        """Point 'Config changed' audit rows at this project."""
        self.ensure_one()
        return {'project_id': self.id}

    def write(self, vals):
        changed, old_snap = self._audit_snapshot(vals)
        result = super().write(vals)
        self._audit_log_changes(changed, old_snap)
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
