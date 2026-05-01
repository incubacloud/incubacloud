from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ._pip_apt_map import resolve_apt_dependencies
from ._repo_requirements import (
    fetch_requirements_txt,
    is_safe_git_ref,
    is_safe_git_sha,
    is_safe_repo_url,
    merge_pip_requirements,
)


class CloudProjectRepo(models.Model):
    _name = 'cloud.project.repo'
    _description = 'Cloud Project Repository'
    _order = 'sequence, id'

    sequence = fields.Integer(default=10)
    project_id = fields.Many2one(
        comodel_name='cloud.project',
        string='Project',
        required=True,
        ondelete='cascade',
    )
    url = fields.Char(
        string='Repository URL',
        required=True,
        help='Full URL of the GitHub/GitLab repository.',
    )
    branch = fields.Char(
        string='Branch',
        default='main',
        help='Branch to use for deployments.',
    )
    addons = fields.Char(
        string='Addons',
        help=(
            'Comma-separated list of addon module names to install from'
            ' this repository. Leave empty to include all addons (*).'
        ),
    )
    excludes = fields.Char(
        string='Exclude Addons',
        help=(
            'Comma-separated list of addon module names to exclude from'
            ' this repository. Only meaningful when Addons is empty (*).'
        ),
    )
    commit_sha = fields.Char(
        string='Pinned Commit',
        help=(
            'When set, the repository is frozen to this exact commit SHA.'
            ' Rebuilds will use this commit instead of the branch tip.'
        ),
    )

    @api.constrains('url', 'branch', 'commit_sha')
    def _check_git_inputs_safe(self):
        for repo in self:
            url = (repo.url or '').strip()
            if url and not is_safe_repo_url(url):
                raise ValidationError(_(
                    "Repository URL '%s' is not a recognised GitHub "
                    "URL. Use the canonical form "
                    "'https://github.com/<owner>/<repo>'.",
                ) % url)
            branch = (repo.branch or '').strip()
            if branch and not is_safe_git_ref(branch):
                raise ValidationError(_(
                    "Branch '%s' contains characters that are not "
                    "allowed (use letters, digits, dots, slashes, "
                    "underscores or hyphens; cannot start with '-' "
                    "or contain '..').",
                ) % branch)
            sha = (repo.commit_sha or '').strip()
            if sha and not is_safe_git_sha(sha):
                raise ValidationError(_(
                    "Pinned commit '%s' is not a valid git SHA "
                    "(7-64 lowercase hex characters).",
                ) % sha)

    @api.constrains('addons', 'excludes')
    def _check_addons_excludes_overlap(self):
        for repo in self:
            inc = {
                m.strip()
                for m in (repo.addons or '').split(',')
                if m.strip()
            }
            exc = {
                m.strip()
                for m in (repo.excludes or '').split(',')
                if m.strip()
            }
            overlap = inc & exc
            if overlap:
                raise ValidationError(_(
                    "Repo %(url)s: module(s) in both Addons and Exclude Addons: %(modules)s",
                ) % {'url': repo.url, 'modules': ', '.join(sorted(overlap))})

    # ── Requirements sync ──────────────────────────────────────────────────

    def _apply_requirements(self):
        """Fetch requirements.txt and merge into the project's
        pip_dependencies, inserting git-style conflict markers where
        the incoming version differs from an existing spec.
        Runs silently — failures are ignored.
        Skipped when context flag ``skip_apply_requirements`` is set.
        """
        if self.env.context.get('skip_apply_requirements'):
            return
        for repo in self:
            if not repo.url or not repo.branch:
                continue
            content = fetch_requirements_txt(
                self.env, repo.url, repo.branch,
            )
            if not content:
                continue
            project = repo.project_id
            result = merge_pip_requirements(
                project.pip_dependencies, content,
                repo_url=repo.url, repo_branch=repo.branch,
            )
            if result['content'] != (project.pip_dependencies or ''):
                project.pip_dependencies = result['content']
                # Auto-add APT build deps for pip packages that need them
                needed = resolve_apt_dependencies(result['content'])
                if needed:
                    existing = set(
                        (project.apt_dependencies or '').split()
                    )
                    new_apt = needed - existing
                    if new_apt:
                        project.apt_dependencies = (
                            (project.apt_dependencies or '').rstrip()
                            + '\n' + '\n'.join(sorted(new_apt))
                        ).strip()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._apply_requirements()
        return records

    def write(self, vals):
        result = super().write(vals)
        if 'url' in vals or 'branch' in vals:
            self._apply_requirements()
        return result
