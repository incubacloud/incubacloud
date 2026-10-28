import itertools
import re

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.fields import Domain
from odoo.tools import SQL, Query, mute_logger
from odoo.exceptions import AccessError, UserError, ValidationError

from . import _odoo_versions
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
_REMOTE_FOLDER_MAX = 63
#: Name of the unique index behind ``_remote_folder_uniq``.
_REMOTE_FOLDER_INDEX = 'cloud_project_remote_folder_uniq'

#: The directories the platform keeps in the home of every host's account:
#: the proxy's routes and certificates, the shared egress whitelist, the
#: warm spares, the observability agents and the central stack. A
#: project's instances live in ``~/<remote folder>/<name>``: a project
#: whose folder were one of these would deploy into, and tear down, the
#: platform's own files. The SaaS layer refuses them as tenant slugs too.
RESERVED_REMOTE_FOLDERS = frozenset({
    'traefik', 'globalwhitelist', 'incubacloud', 'observability',
    'observability-central',
})


class CloudProject(models.Model):
    _name = 'cloud.project'
    _inherit = [
        'cloud.security.mixin',
        'cloud.audit.tracked.mixin',
        'cloud.pip.provenance.mixin',
    ]
    _description = 'Cloud Project'

    name = fields.Char(
        required=True,
        translate=True,
    )
    # One folder per project, whatever its case: the directories of the
    # instances on a host are named after it, so two projects with one
    # would remove and overwrite each other's. Every row is covered, an
    # archived one too if the model ever gets the flag; no folder is no
    # claim. The message is a plain string on purpose: it is reflected
    # into ``ir.model.constraint.message`` and translated from there.
    _remote_folder_uniq = models.UniqueIndex(
        "(lower(remote_folder)) WHERE remote_folder IS NOT NULL AND remote_folder <> ''",
        "The remote folder of a project must be unique, whatever its case: "
        "the directories of its instances on the hosts are named after it. "
        "Choose another remote folder.",
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
    # A new project gets the newest series this panel offers.
    odoo_version = fields.Selection(
        selection=ODOO_VERSION_SELECTION,
        string='Odoo Version',
        default=_odoo_versions.new_record_default,
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
        # Opt-in: a preview is an instance nobody asked for by name,
        # carrying a copy of production data. Existing projects keep
        # whatever they have stored; only new ones start switched off.
        default=False,
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

    @api.constrains('odoo_version')
    def _check_series_may_be_chosen(self):
        """A series the panel does not offer is chosen by an administrator only.

        The pickers only show offered series, but the value also arrives
        by RPC and by import; see ``_odoo_versions.may_choose``.
        """
        pin = _odoo_versions.pinned_template_ref(self.env)
        for project in self:
            if project.odoo_version and not _odoo_versions.may_choose(
                project.odoo_version, self.env.user, pin,
            ):
                # Offered, but not by this panel: its pin is too old.
                if _odoo_versions.card(project.odoo_version)["offered"]:
                    raise ValidationError(_(
                        "Odoo %(version)s is not offered on this panel.",
                        version=project.odoo_version,
                    ))
                raise ValidationError(_(
                    "Odoo %(version)s is not offered yet.",
                    version=project.odoo_version,
                ))

    @api.constrains('remote_folder')
    def _check_remote_folder_shell_safe(self):
        for p in self:
            self._check_remote_folder_shape(p.remote_folder)

    @api.model
    def _check_remote_folder_shape(self, folder):
        """Refuse a remote folder that is not one safe path segment.

        The whole value must match, a line break at its end included:
        the folder is a segment of the paths of the commands run on the
        hosts.

        :param str folder: the folder the project would have; empty is
            no folder
        :raises ValidationError: when it is malformed
        """
        if folder and not _REMOTE_FOLDER_RE.fullmatch(folder):
            raise ValidationError(_(
                "Remote folder '%(folder)s' is invalid. It must start "
                "with a lowercase letter or digit and contain only "
                "lowercase letters, digits, hyphens and underscores "
                "(max 63 chars).",
                folder=folder,
            ))

    # Fields tracked in the audit log when changed
    _AUDIT_TRACKED_FIELDS = frozenset({
        'name', 'odoo_version', 'backup_backend_id',
        'pr_reviews_enabled', 'member_ids',
    })

    @api.model_create_multi
    def create(self, vals_list):
        self._check_can_create_project()
        derived = False
        for vals in vals_list:
            self._check_backup_backend_choice(vals)
            folder = vals.get('remote_folder') or _slugify(vals.get('name') or '')
            self._check_remote_folder_not_reserved(folder)
            if 'remote_folder' not in vals:
                # Derived from the name: not a choice, so it never
                # collides; it takes a number (see below).
                derived = True
            else:
                self._check_remote_folder_free(vals['remote_folder'])
        if not derived:
            return super().create(vals_list)
        # A derived folder is the first free one of ``_derived_remote_folders``.
        # Two requests racing for it both see it free (the snapshot of a
        # transaction does not show what the other commits): the later one
        # meets the unique index, and its create is tried again, in a
        # savepoint, with the next candidate.
        projects = self.browse()
        for vals in vals_list:
            if 'remote_folder' in vals:
                projects |= super().create([vals])
                continue
            for folder in self._derived_remote_folders(vals.get('name')):
                try:
                    with mute_logger('odoo.sql_db'), self.env.cr.savepoint():
                        projects |= super().create(
                            [vals | {'remote_folder': folder}],
                        )
                except IntegrityError as error:
                    if error.diag.constraint_name != _REMOTE_FOLDER_INDEX:
                        raise
                    continue
                break
        return projects

    @api.model
    def _derived_remote_folders(self, name):
        """Yield, in order, the folders a project of this name could take that are free.

        The slug of the name first, then ``<slug>-2``, ``<slug>-3``…,
        shortened to fit the folder's length. Each is checked as a folder
        is where it is set (shape, platform directories) and against the
        folders the database shows taken, whatever their case. The caller
        takes the first and goes on to the next if the insert is refused.

        :param str name: the project's name
        :raises ValidationError: when the slug is malformed or reserved:
            the name is refused, not numbered
        """
        base = _slugify(name)
        self._check_remote_folder_not_reserved(base)
        for number in itertools.count(1):
            suffix = '' if number == 1 else '-%d' % number
            folder = base[:_REMOTE_FOLDER_MAX - len(suffix)] + suffix
            self._check_remote_folder_not_reserved(folder)
            if not self._remote_folder_is_taken(folder):
                yield folder

    @api.model
    def _check_remote_folder_not_reserved(self, folder):
        """Refuse a remote folder the platform keeps for itself on every host.

        Checked where a folder is set — at a create, from the name when
        none is given, as ``_compute_remote_folder`` derives it, and at a
        write — and not as a constraint: a constraint runs again whenever
        the name is written, and the one project of a panel that predates
        this rule could no longer be saved. Applies to everyone, the
        platform included (``RESERVED_REMOTE_FOLDERS``).

        :param str folder: the folder the project would have
        :raises ValidationError: when it is malformed, checked first, or
            reserved
        """
        self._check_remote_folder_shape(folder)
        if folder in RESERVED_REMOTE_FOLDERS:
            raise ValidationError(_(
                "Remote folder '%(folder)s' is reserved for the platform's "
                "own files on every host. Choose another name or remote "
                "folder.",
                folder=folder,
            ))

    def _check_remote_folder_free(self, folder):
        """Refuse a remote folder another project already has.

        Compared without regard to case and over every project, through
        SQL: the caller may not see the project that holds it, and an
        archived one holds it still. The holder is not named. Checked
        where a folder is set, as ``_check_remote_folder_not_reserved``
        is; the unique index behind it (``_remote_folder_uniq``) catches
        what skips that — one folder written to several projects at once.

        :param str folder: the folder the project would have; empty is no
            folder
        :raises ValidationError: when another project has it
        """
        if folder and self._remote_folder_is_taken(folder):
            raise ValidationError(_(
                "Remote folder '%(folder)s' is already used by another "
                "project. The directories of its instances on the hosts are "
                "named after it. Choose another name or remote folder.",
                folder=folder,
            ))

    def _remote_folder_is_taken(self, folder):
        """Tell whether a project other than ``self`` has this remote folder.

        Compared without regard to case and over every project, through
        SQL: the caller may not see the project that holds it, and an
        archived one holds it still.

        :param str folder: the folder to look for
        :returns: True when another project has it
        """
        self.env['cloud.project'].flush_model(['remote_folder'])
        self.env.cr.execute(
            "SELECT 1 FROM cloud_project "
            "WHERE lower(remote_folder) = lower(%s) AND id != ALL(%s) LIMIT 1",
            [folder, self.ids or [0]],
        )
        return bool(self.env.cr.fetchone())

    def _check_backup_backend_choice(self, vals):
        """Refuse choosing where a project's backups are kept below Developer.

        The project's backend is where its instances' backups go unless
        an instance names its own: backups are a developer's, as they are
        on the instance (``cloud.instance._check_guarded_vals``). Saved as
        it is, or empty on a new project, it is no choice.

        :param dict vals: the values of the create, or of the write of
            ``self``
        :raises AccessError: when a caller below Developer changes it
        """
        if (
            self.env.su
            or 'backup_backend_id' not in vals
            or self._has_cloud_group('group_cloud_developer')
        ):
            return
        backend = vals['backup_backend_id'] or False
        if any(p.backup_backend_id.id != backend for p in self) or (
            not self and backend
        ):
            raise AccessError(_(
                "Only a Developer or an Administrator may choose where "
                "backups are kept.",
            ))

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
        # A project without a folder takes one from its new name
        # (``_compute_remote_folder``): that rename chooses the folder,
        # and is checked below as choosing it.
        bare = self.filtered(lambda p: not p.remote_folder)
        derived = False
        if 'name' in vals and 'remote_folder' not in vals and bare:
            if bare != self:
                return (self - bare).write(vals) and bare.write(vals)
            if len(self) > 1:
                # One name, but each project needs a folder of its own.
                return all([project.write(vals) for project in self])
            vals = vals | {'remote_folder': _slugify(vals['name'] or '')}
            derived = True
        self._check_backup_backend_choice(vals)
        if 'remote_folder' in vals:
            for project in self:
                # Emptied, it is derived again from the name.
                self._check_remote_folder_not_reserved(
                    vals['remote_folder']
                    or _slugify(vals.get('name') or project.name or ''),
                )
        # The directories of a project's instances are named after its
        # remote folder (``cloud.instance.get_remote_dir``): renamed to
        # another project's, a teardown would remove that project's
        # instances, and a deploy write into them. An administrator's.
        if (
            'remote_folder' in vals
            and not self.env.su
            and any(p.remote_folder != vals['remote_folder'] for p in self)
            and not self._has_cloud_group('group_cloud_manager')
        ):
            raise AccessError(_(
                "Only an Administrator may change a project's remote "
                "folder: the directories of its instances on their hosts "
                "are named after it.",
            ))
        # After the Administrator's gate: a caller refused that does not
        # learn which folders are taken.
        if derived:
            # Chosen by the rename, from the name: numbered when taken.
            vals = vals | {'remote_folder': next(
                self._derived_remote_folders(vals['name']),
            )}
        if 'remote_folder' in vals:
            for project in self:
                project._check_remote_folder_free(
                    vals['remote_folder']
                    or _slugify(vals.get('name') or project.name or ''),
                )
        # A form sends the series back on every save: unchanged, it is
        # not a choice for ``_check_series_may_be_chosen`` to judge.
        vals = _odoo_versions.without_unchanged_series(self, vals)
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
