from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ._pip_apt_map import resolve_apt_dependencies
from ._repo_requirements import fetch_requirements_txt, merge_pip_requirements


class CloudInstanceRepo(models.Model):
    _name = 'cloud.instance.repo'
    _description = 'Cloud Instance Repository'
    _order = 'sequence, id'

    sequence = fields.Integer(default=10)
    instance_id = fields.Many2one(
        comodel_name='cloud.instance',
        string='Instance',
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
                raise ValidationError(
                    f"Repo {repo.url}: module(s) in both Addons and"
                    f" Exclude Addons: {', '.join(sorted(overlap))}"
                )

    # ── Requirements sync ──────────────────────────────────────────────────

    def _apply_requirements(self):
        """Fetch requirements.txt and merge into the instance's
        pip_dependencies, inserting git-style conflict markers where
        the incoming version differs from an existing spec.
        Runs silently — failures are ignored.
        Skipped when context flag ``skip_apply_requirements`` is set
        (e.g. from save_instance, where the frontend already merged).
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
            instance = repo.instance_id
            result = merge_pip_requirements(
                instance.pip_dependencies, content,
                repo_url=repo.url, repo_branch=repo.branch,
            )
            if result['content'] != (instance.pip_dependencies or ''):
                instance.pip_dependencies = result['content']
                needed = resolve_apt_dependencies(result['content'])
                if needed:
                    existing = set(
                        (instance.apt_dependencies or '').split()
                    )
                    new_apt = needed - existing
                    if new_apt:
                        instance.apt_dependencies = (
                            (instance.apt_dependencies or '').rstrip()
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
