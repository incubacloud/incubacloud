import hashlib
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..github.client import GitHubAppClient
from .cloud_host import parse_memory_to_gb
from .encrypted_char import EncryptedChar
from .password_utils import generate_password
from ._repo_requirements import _normalize_url

_logger = logging.getLogger(__name__)
_GLOBAL_BACKUP_PARAM = 'incubacloud.backup_backend_id'

# Docker compose project names / COMPOSE_PROJECT_NAME, DNS labels and the
# path segment used by executors must be lowercase alnum + [_-], 1-63 chars.
# Blocks every shell metachar (;|&$`"' space \) and path traversal (/ ..).
_INSTANCE_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}$')

# ~/..., /..., or relative path. Each segment is [A-Za-z0-9._-]+ and we
# explicitly reject '..' segments. No shell metacharacters.
_REMOTE_DIR_RE = re.compile(
    r'^(~/|/)?[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/?$'
)

# PostgreSQL identifier (database / role name). Used in psql -U/-d
# arguments that flow through SSH shell to docker compose exec, so we
# must keep them free of any character bash or psql could misinterpret.
# 1-63 chars, lowercase letter or underscore start, then alnum + '_'.
_PG_IDENT_RE = re.compile(r'^[a-z_][a-z0-9_]{0,62}$')


class CloudInstance(models.Model):
    _name = 'cloud.instance'
    _inherit = ['cloud.security.mixin']
    _description = 'Cloud Instance'

    _name_project_uniq = models.Constraint(
        'unique (project_id, name)',
        'Instance name must be unique within a project.',
    )
    _one_production_per_project = models.Constraint(
        "EXCLUDE (project_id WITH =) WHERE (environment = 'production' AND project_id IS NOT NULL)",
        'A project can only have one production instance.',
    )

    active = fields.Boolean(default=True)
    auto_rebuild = fields.Boolean(
        string='Auto-Rebuild on Push',
        default=False,
        help='Automatically trigger a rebuild when a push event is '
             'received on a matching (unfrozen) repo branch.',
    )
    last_auto_rebuild = fields.Datetime(
        string='Last Auto-Rebuild',
        readonly=True,
    )
    auto_update = fields.Boolean(
        string='Auto-Update Modules on Rebuild',
        default=True,
        help='Run click-odoo-update during rebuild to apply module '
             'schema/data changes. Disable for instances that manage '
             'module state manually or cannot accept automatic updates '
             '(e.g. frozen production with a change-management policy). '
             'Disabling also skips the safe boot test that relies on '
             'click-odoo-update.',
    )

    # ── Smart rebuild fingerprint ─────────────────────────────────────────
    rebuild_fingerprint = fields.Char(
        string='Rebuild Fingerprint',
        compute='_compute_rebuild_fingerprint',
        store=True,
        help='Hash of fields that affect the Docker image. When this '
             'differs from last_rebuild_fingerprint, a full rebuild '
             '(--pull --no-cache) is triggered instead of a cached one.',
    )
    last_rebuild_fingerprint = fields.Char(
        string='Last Rebuild Fingerprint',
        readonly=True,
        help='Fingerprint saved after the last successful rebuild/deploy.',
    )

    name = fields.Char(
        required=True,
        translate=True,
    )
    doodba_project_name = fields.Char(
        compute='_compute_doodba_project_name',
        string='Doodba Project Name',
        help='Composite name used as copier project_name: '
             '{remote_folder}-{name}. Ensures unique Docker '
             'container names across projects on the same host.',
    )
    project_id = fields.Many2one(
        comodel_name='cloud.project',
        string='Project',
    )
    host_id = fields.Many2one(
        comodel_name='cloud.host',
        string='Host',
    )
    status = fields.Selection(
        selection=[
            ('ok', 'OK'),
            ('warning', 'Warning'),
            ('error', 'Error'),
            ('provisioning', 'Provisioning'),
        ],
        string='Status',
        required=True,
        default='ok',
    )
    deployed = fields.Boolean(string='Deployed', default=False)
    running = fields.Boolean(string='Running', default=False)
    custom_remote_dir = fields.Char(
        string='Custom Remote Directory',
        help='Override for the remote directory path. '
             'When set, used instead of the computed ~/project/instance path. '
             'Set automatically when importing existing instances.',
    )
    environment = fields.Selection(
        selection=[
            ('staging', 'Staging'),
            ('production', 'Production'),
        ],
        string='Environment',
        required=True,
        default='staging',
    )
    tag_ids = fields.Many2many(
        comodel_name='cloud.instance.tag',
        relation='cloud_instance_tag_rel',
        column1='instance_id',
        column2='tag_id',
        string='Tags',
    )

    # ── PR preview fields ────────────────────────────────────────────────────
    pr_number = fields.Integer(
        string='PR Number',
        index=True,
        help='GitHub PR number that created this instance. '
             'When set, this instance is a PR preview — auto-destroyed on PR close.',
    )
    pr_repo = fields.Char(
        string='PR Repository',
        help='Full repository name (owner/repo) of the PR.',
    )
    pr_head_branch = fields.Char(
        string='PR Branch',
        help='Head branch of the PR.',
    )
    pr_comment_id = fields.Integer(
        string='GitHub Comment ID',
        help='ID of the GitHub issue comment posted on the PR.',
    )

    # ── Copier template parameters ──────────────────────────────────────────

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
    )
    odoo_commit_sha = fields.Char(
        string='Pinned Odoo Commit',
        help=(
            'When set, the Odoo core repository is frozen to this exact'
            ' commit SHA instead of following the branch tip.'
        ),
    )
    odoo_initial_lang = fields.Many2one(
        'res.lang',
        string='Initial Language',
        context={'active_test': False},
        help='Language installed via odoo -i base -l <code> on first deploy.',
    )
    odoo_admin_password = EncryptedChar(
        string='Admin Password',
        groups='incubacloud.group_cloud_developer',
    )
    odoo_admin_user_password = EncryptedChar(
        string='Admin User Password',
        groups='incubacloud.group_cloud_developer',
    )
    odoo_proxy = fields.Selection(
        [('traefik', 'Traefik'), ('none', 'None')],
        string='Proxy', default='traefik',
    )

    postgres_version = fields.Selection(
        [('14', '14'), ('15', '15'), ('16', '16'), ('17', '17'), ('18', '18')],
        string='PostgreSQL Version', default='17',
    )
    postgres_dbname = fields.Char(string='DB Name', default='prod')
    postgres_username = fields.Char(string='DB User', default='odoo')
    postgres_password = EncryptedChar(
        string='DB Password',
        groups='incubacloud.group_cloud_developer',
    )

    domain_ids = fields.One2many(
        'cloud.instance.domain', 'instance_id', string='Domains',
    )
    domain = fields.Char(
        string='Primary Domain',
        compute='_compute_domain', store=True,
    )

    @api.depends('name', 'project_id.remote_folder')
    def _compute_doodba_project_name(self):
        for inst in self:
            folder = (
                inst.project_id.remote_folder
                if inst.project_id
                else ''
            )
            if folder:
                inst.doodba_project_name = f"{folder}-{inst.name}"
            else:
                inst.doodba_project_name = inst.name

    def get_remote_dir(self):
        """Return the remote directory path for this instance.

        Uses ``custom_remote_dir`` if set (imported instances),
        otherwise computes ~/project_remote_folder/instance_name/.
        """
        self.ensure_one()
        if self.custom_remote_dir:
            return self.custom_remote_dir
        project_folder = (
            self.project_id.remote_folder if self.project_id else ''
        ) or 'instances'
        return f"~/{project_folder}/{self.name}"

    @api.depends('domain_ids.hostname', 'domain_ids.sequence')
    def _compute_domain(self):
        # Sort explicitly by (sequence, id) instead of relying on
        # cloud.instance.domain._order. The value of ``inst.domain``
        # flows into _base_url() and from there into psql -c "..." in
        # deploy/rebuild executors (see H-MOD-2 fix). Sorting here
        # makes the resolution independent of any future change to
        # the related model's _order, so a refactor cannot silently
        # alter which hostname becomes the primary domain.
        for inst in self:
            domains = inst.domain_ids.sorted(
                key=lambda d: (d.sequence, d.id),
            )
            first = domains[:1]
            inst.domain = first.hostname if first else ''

    odoo_admin_email = fields.Char(string='Admin Email')

    smtp_relay_host = fields.Char(string='SMTP Host')
    smtp_relay_port = fields.Integer(string='SMTP Port', default=587)
    smtp_relay_security = fields.Selection(
        selection=[
            ('none', 'None'),
            ('starttls', 'STARTTLS'),
            ('ssl', 'SSL/TLS'),
        ],
        string='SMTP Security',
        default='starttls',
    )
    smtp_relay_user = fields.Char(string='SMTP User')
    smtp_relay_password = EncryptedChar(
        string='SMTP Password',
        groups='incubacloud.group_cloud_developer',
    )

    # ── Resource limits (docker-compose.override.yml) ─────────────────
    odoo_memory_limit = fields.Char(
        string='Odoo Memory Limit', default='2g',
        help='Docker mem_limit for odoo service (e.g. 2g, 512m)',
    )
    odoo_cpus = fields.Float(string='Odoo CPUs', default=2.0)
    db_memory_limit = fields.Char(
        string='DB Memory Limit', default='1g',
    )
    db_cpus = fields.Float(string='DB CPUs', default=1.0)
    backup_memory_limit = fields.Char(
        string='Backup Memory Limit', default='512m',
    )
    backup_cpus = fields.Float(string='Backup CPUs', default=0.5)
    smtp_memory_limit = fields.Char(
        string='SMTP Memory Limit', default='256m',
    )
    smtp_cpus = fields.Float(string='SMTP CPUs', default=0.25)

    odoo_conf = fields.Text(
        string='Odoo Configuration',
        default=(
            '[options]\n'
            'workers = 2\n'
            'server_wide_modules = web\n'
            'limit_time_cpu = 600\n'
            'limit_time_real = 1200\n'
            'max_cron_threads = 1\n'
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

    repo_ids = fields.One2many(
        comodel_name='cloud.instance.repo',
        inverse_name='instance_id',
        string='Repositories',
    )

    alert_ids = fields.One2many(
        comodel_name='cloud.alert',
        inverse_name='instance_id',
        string='Alerts',
    )

    last_health_check = fields.Datetime(
        string='Last Health Check',
        readonly=True,
    )

    compose_services = fields.Char(
        string='Compose Services',
        default='odoo,db',
        help=(
            'Comma-separated list of Docker Compose service names'
            ' detected from the YAML.'
        ),
    )

    backup_backend_id = fields.Many2one(
        comodel_name='cloud.backup.backend',
        string='Backup Backend',
        help='Backup backend for this instance. Overrides the project default.',  # noqa: E501
    )
    effective_backup_backend = fields.Many2one(
        comodel_name='cloud.backup.backend',
        string='Effective Backup Backend',
        compute='_compute_effective_backup_backend',
    )

    instance_backup_dst = fields.Char(
        string='Instance Backup Destination',
        compute='_compute_instance_backup_dst',
        help='Per-instance duplicity DST: {backend_dst}/{project}/{instance}',
    )
    custom_backup_dst = fields.Char(
        string='Custom Backup Destination',
        help='Override for the backup destination path. '
             'When set, used instead of the computed path. '
             'Set automatically when importing existing instances.',
    )

    @api.depends('backup_backend_id', 'project_id.backup_backend_id')
    def _compute_effective_backup_backend(self):
        ICP = self.env['ir.config_parameter'].sudo()
        global_id = int(ICP.get_param(_GLOBAL_BACKUP_PARAM, 0) or 0)
        Backend = self.env['cloud.backup.backend']
        global_backend = Backend.browse(global_id) if global_id else Backend
        for inst in self:
            inst.effective_backup_backend = (
                inst.backup_backend_id
                or inst.project_id.backup_backend_id
                or global_backend
            )

    @api.depends(
        'custom_backup_dst',
        'effective_backup_backend', 'name', 'project_id.remote_folder',
    )
    def _compute_instance_backup_dst(self):
        for inst in self:
            if inst.custom_backup_dst:
                inst.instance_backup_dst = inst.custom_backup_dst
                continue
            bb = inst.effective_backup_backend
            if bb and bb.backup_dst and inst.name:
                project_folder = (
                    inst.project_id.remote_folder
                    if inst.project_id else ''
                ) or 'default'
                inst.instance_backup_dst = (
                    f"{bb.backup_dst}/{project_folder}/{inst.name}"
                )
            else:
                inst.instance_backup_dst = ''

    @api.depends(
        'odoo_version', 'odoo_commit_sha', 'pip_dependencies',
        'apt_dependencies', 'odoo_proxy',
        'repo_ids.url', 'repo_ids.branch', 'repo_ids.commit_sha',
        'repo_ids.addons', 'repo_ids.excludes',
    )
    def _compute_rebuild_fingerprint(self):
        for inst in self:
            parts = [
                inst.odoo_version or '',
                inst.odoo_commit_sha or '',
                inst.pip_dependencies or '',
                inst.apt_dependencies or '',
                inst.odoo_proxy or '',
            ]
            for repo in inst.repo_ids.sorted('id'):
                parts.extend([
                    repo.url or '',
                    repo.branch or '',
                    repo.commit_sha or '',
                    repo.addons or '',
                    repo.excludes or '',
                ])
            raw = '|'.join(parts)
            inst.rebuild_fingerprint = (
                hashlib.sha256(raw.encode()).hexdigest()[:16]
            )

    def write(self, vals):
        # Drop empty password values so existing stored passwords are preserved
        for field in self._PASSWORD_FIELDS:
            if field in vals and not vals[field]:
                del vals[field]
        # Snapshot tracked fields before write
        changed = self._AUDIT_TRACKED_FIELDS & set(vals)
        old_snap = {}
        if changed:
            old_snap = {f: {r.id: r[f] for r in self} for f in changed}
        result = super().write(vals)
        # Auto-generate domain when a host is assigned to an instance
        # that doesn't have any domains yet (e.g. imported without host).
        if 'host_id' in vals and vals['host_id']:
            for inst in self:
                if inst.domain_ids:
                    continue
                host = inst.host_id
                if not host.wildcard_domain:
                    continue
                project = inst.project_id
                if project and project.remote_folder:
                    subdomain = f"{project.remote_folder}-{inst.name}"
                else:
                    subdomain = inst.name
                hostname = f"{subdomain}.{host.wildcard_domain}"
                self.env['cloud.instance.domain'].create({
                    'instance_id': inst.id,
                    'hostname': hostname,
                })
        # Write audit log entries for changed tracked fields
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
                        'instance_id': rec.id,
                        'host_id': rec.host_id.id if rec.host_id else False,
                        'details': '; '.join(parts)[:255],
                    })
        return result

    def _check_backup_backend(self):
        """Raise if this production instance has no effective backup backend."""
        if self.environment == 'production' and not self.effective_backup_backend:
            raise ValueError(_(
                "No backup backend configured for this instance. "
                "Set one at instance level, project level, or globally "
                "in Settings → General → Default Backup Backend."
            ))

    def deploy(self):
        """Enqueue a deploy_instance job for this instance."""
        self.ensure_one()
        if not self.host_id:
            raise ValueError("Instance has no host assigned.")
        self._check_backup_backend()
        return self.env['cloud.job'].enqueue(
            self.host_id.id,
            self.id,
            'deploy_instance',
        )

    def list_backups(self):
        """Enqueue a backup_list job for this instance."""
        self.ensure_one()
        if not self.host_id:
            raise ValueError("Instance has no host assigned.")
        if self.environment == 'production':
            self._check_backup_backend()
        return self.env['cloud.job'].enqueue(
            self.host_id.id,
            self.id,
            'backup_list',
        )

    def create_backup(self):
        """Enqueue backup_create → backup_list chain.

        Returns the trailing backup_list job id so callers polling for
        completion only see ``done`` once the records have been synced
        from duplicity.
        """
        self.ensure_one()
        if not self.host_id:
            raise ValueError("Instance has no host assigned.")
        if not self.deployed:
            raise ValueError("Instance is not deployed.")
        if self.environment == 'production':
            self._check_backup_backend()
        step = {'host_id': self.host_id.id, 'instance_id': self.id}
        ids = self.env['cloud.job'].enqueue_chain([
            {**step, 'job_type_code': 'backup_create'},
            {**step, 'job_type_code': 'backup_list'},
        ])
        return ids[-1]

    def download_backup(self, payload):
        """Enqueue a backup_download job for this instance."""
        self.ensure_one()
        if not self.host_id:
            raise ValueError("Instance has no host assigned.")
        self._check_backup_backend()
        return self.env['cloud.job'].enqueue(
            self.host_id.id,
            self.id,
            'backup_download',
            payload=payload,
        )

    def download_backup_neutralized(self, payload):
        """Enqueue a backup_download_neutralized job for this instance.

        Unlike ``download_backup`` (prod-only, driven by duplicity), this
        works for both prod (``time`` = duplicity timestamp) and non-prod
        (``time`` = 'live', dumps the current DB on the fly).
        """
        self.ensure_one()
        if not self.host_id:
            raise ValueError("Instance has no host assigned.")
        if not self.deployed:
            raise ValueError("Instance is not deployed.")
        if self.environment == 'production':
            self._check_backup_backend()
        return self.env['cloud.job'].enqueue(
            self.host_id.id,
            self.id,
            'backup_download_neutralized',
            payload=payload,
        )

    def restore_backup(self, payload):
        """Enqueue a backup_restore job for this instance."""
        self.ensure_one()
        if not self.host_id:
            raise ValueError("Instance has no host assigned.")
        self._check_backup_backend()
        return self.env['cloud.job'].enqueue(
            self.host_id.id,
            self.id,
            'backup_restore',
            payload=payload,
        )

    def clone_to_staging(self, staging_name,
                         pr_number=0, pr_repo='', pr_head_branch=''):
        """Create a staging copy of this production instance with its data.

        When *pr_number* is provided the clone is a PR preview instance:
        the repo whose URL matches *pr_repo* has its branch set to
        *pr_head_branch* so the PR code is deployed instead of the
        default branch.
        """
        self.ensure_one()
        if self.environment != 'production':
            raise ValueError("Only production instances can be cloned.")
        if not self.deployed:
            raise ValueError("Instance must be deployed first.")

        vals = {
            'name': staging_name,
            'project_id': self.project_id.id,
            'host_id': self.host_id.id,
            'environment': 'staging',
            'status': 'provisioning',
            'odoo_version': self.odoo_version,
            'odoo_commit_sha': self.odoo_commit_sha,
            'odoo_initial_lang': (
                self.odoo_initial_lang.id
                if self.odoo_initial_lang else False
            ),
            'odoo_proxy': self.odoo_proxy,
            'postgres_version': self.postgres_version,
            'postgres_dbname': 'prod',
            'postgres_username': self.postgres_username,
            'smtp_relay_host': self.smtp_relay_host,
            'smtp_relay_port': self.smtp_relay_port,
            'smtp_relay_security': self.smtp_relay_security,
            'smtp_relay_user': self.smtp_relay_user,
            'odoo_conf': self.odoo_conf,
            'pip_dependencies': self.pip_dependencies,
            'apt_dependencies': self.apt_dependencies,
        }
        if pr_number:
            vals |= {
                'pr_number': pr_number,
                'pr_repo': pr_repo,
                'pr_head_branch': pr_head_branch,
            }
        staging = self.with_context(
            skip_project_repos=True,
        ).create(vals)

        pr_repo_norm = _normalize_url(
            f'https://github.com/{pr_repo}' if pr_repo else ''
        )
        for repo in self.repo_ids:
            new_repo = repo.copy({'instance_id': staging.id})
            if pr_head_branch and pr_repo_norm:
                if _normalize_url(new_repo.url) == pr_repo_norm:
                    new_repo.write({'branch': pr_head_branch, 'commit_sha': False})

        job_ids = self.env['cloud.job'].enqueue_chain([
            {
                'host_id': self.host_id.id,
                'instance_id': staging.id,
                'job_type_code': 'deploy_instance',
            },
            {
                'host_id': self.host_id.id,
                'instance_id': self.id,  # prod
                'job_type_code': 'backup_download',
                'payload': {
                    'time': 'latest',
                    'download_type': 'all',
                },
            },
            {
                'host_id': self.host_id.id,
                'instance_id': staging.id,
                'job_type_code': 'restore_instance',
                'payload': {
                    'mode': 'from_job',
                    'source_job_id': '__chain_job_1__',
                },
            },
        ])
        return {'staging_id': staging.id, 'job_id': job_ids[0]}

    def _post_or_update_pr_comment(self, body):
        """Post or update the GitHub PR comment for this PR preview instance."""
        if not self.pr_number or not self.pr_repo:
            return
        owner, repo = self.pr_repo.split('/', 1)
        creds = self.env['cloud.github.credential.service'].get_credentials()
        if not creds:
            return
        client = GitHubAppClient(creds)
        try:
            if self.pr_comment_id:
                client.patch_issue_comment(owner, repo, self.pr_comment_id, body)
            else:
                result = client.post_issue_comment(owner, repo, self.pr_number, body)
                if result.get('id'):
                    self.write({'pr_comment_id': result['id']})
        except Exception:
            _logger.exception("PR comment update failed for %s", self.name)

    def _delete_pr_comment(self):
        """Delete the GitHub PR comment for this PR preview instance."""
        if not self.pr_comment_id or not self.pr_repo:
            return
        owner, repo = self.pr_repo.split('/', 1)
        creds = self.env['cloud.github.credential.service'].get_credentials()
        if not creds:
            return
        client = GitHubAppClient(creds)
        try:
            client.delete_issue_comment(owner, repo, self.pr_comment_id)
            self.write({'pr_comment_id': 0})
        except Exception:
            _logger.exception("PR comment delete failed for %s", self.name)

    def restore_db(self, payload):
        """Enqueue a restore_instance job with the given payload.

        ``payload['mode']`` selects the source of the backup zip:

        * ``browser``  — operator uploaded a zip via /cloud/instance/<id>/restore;
                         the controller stored it under tempfile.mkstemp() and
                         passes the resulting path as ``local_path``.
        * ``from_job`` — the zip is an ir.attachment of a previous cloud.job
                         (typically a backup_download); the executor pulls it
                         from the DB.
        * ``rsync``    — the zip is already on the remote host at the path
                         the executor expects (operator uploaded it manually).

        The method is exposed via JSON-RPC, so we gate it explicitly:
        only Developer+ can trigger a restore. The downstream executor
        also validates the ``local_path`` prefix as a defense-in-depth
        layer (see RestoreInstanceExecutor.before_execute).
        """
        self.ensure_one()
        self.env['cloud.security.mixin']._check_can_manage_backups()
        if not self.host_id:
            raise ValueError("Instance has no host assigned.")
        if (payload or {}).get('mode') not in ('browser', 'from_job', 'rsync'):
            raise ValueError("Invalid restore mode.")
        return self.env['cloud.job'].enqueue(
            self.host_id.id,
            self.id,
            'restore_instance',
            payload=payload,
        )

    @api.model
    def cron_refresh_backup_list(self):
        """Queue a backup_list job for every production instance."""
        instances = self.search([
            ('deployed', '=', True),
            ('environment', '=', 'production'),
        ])
        for inst in instances:
            if not inst.host_id or not inst.instance_backup_dst:
                continue
            try:
                inst.list_backups()
            except Exception:
                _logger.warning(
                    "Could not enqueue backup_list for %s",
                    inst.name, exc_info=True,
                )

    @api.model
    def cron_instance_health(self):
        """Queue an instance_health job for every deployed instance."""
        instances = self.search([('deployed', '=', True)])
        for inst in instances:
            if not inst.host_id:
                continue
            try:
                self.env['cloud.job'].enqueue(
                    inst.host_id.id, inst.id, 'instance_health',
                )
            except Exception as e:
                _logger.warning(
                    "Could not enqueue instance_health for %s: %s",
                    inst.name, e,
                )

    # ── Resource estimation for auto-assign ───────────────────────────────
    _RESOURCE_DEFAULTS = {
        'odoo_cpus': 2.0, 'db_cpus': 1.0,
        'backup_cpus': 0.5, 'smtp_cpus': 0.25,
        'odoo_memory_limit': '2g', 'db_memory_limit': '1g',
        'backup_memory_limit': '512m', 'smtp_memory_limit': '256m',
    }

    @staticmethod
    def _compute_instance_resources(vals):
        """Return (total_cpus, total_ram_gb) from creation vals with defaults."""
        d = CloudInstance._RESOURCE_DEFAULTS
        cpus = sum(
            float(vals.get(k, d[k]) or d[k])
            for k in ('odoo_cpus', 'db_cpus', 'backup_cpus', 'smtp_cpus')
        )
        ram = sum(
            parse_memory_to_gb(vals.get(k, d[k]) or d[k])
            for k in (
                'odoo_memory_limit', 'db_memory_limit',
                'backup_memory_limit', 'smtp_memory_limit',
            )
        )
        return cpus, ram

    # Fields tracked in the audit log when changed (never passwords)
    _AUDIT_TRACKED_FIELDS = frozenset({
        'name', 'host_id', 'project_id', 'environment', 'odoo_version',
        'smtp_relay_host', 'smtp_relay_port', 'smtp_relay_security',
        'odoo_proxy', 'postgres_version', 'postgres_dbname',
        'backup_backend_id', 'active',
    })

    # Password fields: auto-generated if empty on create, preserved on write
    _PASSWORD_FIELDS = (
        'odoo_admin_password', 'odoo_admin_user_password',
        'postgres_password', 'smtp_relay_password',
    )
    # Subset that must always have a value (auto-generated if not provided)
    _REQUIRED_PASSWORD_FIELDS = (
        'odoo_admin_password', 'odoo_admin_user_password', 'postgres_password',
    )

    # ── Validation: name + custom_remote_dir fluye a shell en executors ───
    # Un valor con shell metachars (; | & $ ` " ' space \ etc.) permite
    # command injection via SSH en el host remoto. El regex estricto es la
    # defensa principal; los executors pueden seguir usando f-strings.

    @api.constrains('name')
    def _check_name_shell_safe(self):
        for inst in self:
            if not _INSTANCE_NAME_RE.match(inst.name or ''):
                raise ValidationError(_(
                    "Instance name '%(name)s' is invalid. It must start with "
                    "a lowercase letter or digit and contain only lowercase "
                    "letters, digits, hyphens and underscores (max 63 chars).",
                    name=inst.name,
                ))

    @api.constrains('custom_remote_dir')
    def _check_custom_remote_dir_shell_safe(self):
        for inst in self:
            v = inst.custom_remote_dir
            if not v:
                continue
            if not _REMOTE_DIR_RE.match(v) or '..' in v.split('/'):
                raise ValidationError(_(
                    "Custom remote directory '%(path)s' is invalid. Use only "
                    "letters, digits, dots, hyphens and underscores in each "
                    "path segment. No '..', spaces or shell metacharacters.",
                    path=v,
                ))

    @api.constrains('postgres_dbname', 'postgres_username')
    def _check_pg_identifiers_shell_safe(self):
        for inst in self:
            for fname, label in (
                ('postgres_dbname', 'PostgreSQL database name'),
                ('postgres_username', 'PostgreSQL user'),
            ):
                v = inst[fname]
                if v and not _PG_IDENT_RE.match(v):
                    raise ValidationError(_(
                        "%(label)s '%(value)s' is invalid. Use a valid "
                        "PostgreSQL identifier (1-63 chars, start with a "
                        "lowercase letter or underscore, then letters, "
                        "digits and underscores).",
                        label=label,
                        value=v,
                    ))

    def unlink(self):
        for inst in self:
            self._check_can_delete_instance(inst)
            self.env['cloud.audit.log'].sudo().create({
                'action': 'Instance deleted',
                'host_id': inst.host_id.id if inst.host_id else False,
                'project_id': inst.project_id.id if inst.project_id else False,
                'details': inst.name,
            })
        return super().unlink()

    @api.model_create_multi
    def create(self, vals_list):
        self._check_can_create_instance()
        for vals in vals_list:
            for field in self._REQUIRED_PASSWORD_FIELDS:
                if not vals.get(field):
                    vals[field] = generate_password()
            project_id = vals.get('project_id')
            if project_id:
                project = self.env['cloud.project'].browse(project_id)
                # Copy project dependencies to instance if not set
                if 'pip_dependencies' not in vals and project.pip_dependencies:
                    vals['pip_dependencies'] = project.pip_dependencies
                if 'apt_dependencies' not in vals and project.apt_dependencies:
                    vals['apt_dependencies'] = project.apt_dependencies
                # Auto-generate domain from wildcard
                if (not vals.get('domain_ids')
                        and vals.get('host_id') and vals.get('name')):
                    host = self.env['cloud.host'].browse(vals['host_id'])
                    if host.wildcard_domain:
                        project_folder = project.remote_folder or ''
                        subdomain = (
                            f"{project_folder}-{vals['name']}"
                            if project_folder
                            else vals['name']
                        )
                        hostname = f"{subdomain}.{host.wildcard_domain}"
                        vals['domain_ids'] = [
                            (0, 0, {'hostname': hostname}),
                        ]
            elif (not vals.get('domain_ids')
                    and vals.get('host_id') and vals.get('name')):
                host = self.env['cloud.host'].browse(vals['host_id'])
                if host.wildcard_domain:
                    hostname = f"{vals['name']}.{host.wildcard_domain}"
                    vals['domain_ids'] = [
                        (0, 0, {'hostname': hostname}),
                    ]
        # sudo on create: encrypted password fields are restricted to
        # group_cloud_developer but consultants (lower in the hierarchy)
        # are allowed to create instances. We auto-generate those passwords
        # above; writing them requires sudo. We drop back to the caller's
        # env so the returned recordset respects their normal permissions.
        records = super(
            CloudInstance, self.sudo()
        ).create(vals_list).with_env(self.env)
        for inst in records:
            self.env['cloud.audit.log'].sudo().create({
                'action': 'Instance created',
                'instance_id': inst.id,
                'host_id': inst.host_id.id if inst.host_id else False,
                'project_id': inst.project_id.id if inst.project_id else False,
            })
        # skip_apply_requirements: requirements were already merged into the
        # project's pip_dependencies (and inherited by the instance above).
        # Re-fetching from GitHub here would just duplicate the work and block
        # the request with N synchronous HTTP calls.
        Repo = self.env['cloud.instance.repo'].with_context(
            skip_apply_requirements=True,
        )
        for inst in records:
            if self.env.context.get('skip_project_repos'):
                continue  # caller will create repos explicitly
            for repo in inst.project_id.repo_ids:
                Repo.create({
                    'instance_id': inst.id,
                    'sequence': repo.sequence,
                    'url': repo.url,
                    'branch': repo.branch or 'main',
                    'addons': repo.addons or '',
                    'excludes': repo.excludes or '',
                    'commit_sha': repo.commit_sha or '',
                })
        return records
