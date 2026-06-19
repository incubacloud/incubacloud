"""GitHub-related endpoints: App configuration, repo introspection and
project import from a GitHub repository.

Mixed into ``CloudDataLoadController`` in ``data_load.py``.
"""
import ast
import base64
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

from ...github.client import GitHubAppClient, GitHubAPIError, GitHubPATClient
from ...github.http_utils import safe_urlopen
from ...models._odoo_versions import ODOO_VERSIONS
from ._helpers import (
    _gh_seg, _has_encrypted, _has_pat, _parse_github_repo_path,
)
from .._safe_error import safe_error_response

_logger = logging.getLogger(__name__)

# Allowlist of git hosts we will clone from. Validated by
# ``_build_clone_url`` and ``_resolve_url`` so an attacker-controlled
# ``repo_url`` (or ``.gitmodules`` URL) cannot redirect git clone to
# an arbitrary host (SSRF / supply-chain).
_ALLOWED_GIT_HOSTS = ('github.com', 'www.github.com')

# Odoo series we recognise when determining the version of an imported
# project. Derived from the single source of truth shared with the
# ``odoo_version`` selector on cloud.project / cloud.instance so the
# importer and the selector can never drift apart.
_SUPPORTED_VERSIONS = frozenset(ODOO_VERSIONS)

# Blob-less clone budget when resolving the real branch of a pinned SHA.
_SUBRES_CLONE_TIMEOUT = 30

_SSH_URL_RE = re.compile(r'^git@([^:]+):(.+?)(?:\.git)?$')


def _to_https(url):
    """Normalise a git remote (``git@host:path`` / https) to an HTTPS URL."""
    url = (url or '').strip()
    m = _SSH_URL_RE.match(url)
    if m:
        return f'https://{m.group(1)}/{m.group(2)}.git'
    return url


def _version_from_manifest_text(text):
    """Return the Odoo series ('18.0') from an ``__manifest__.py`` source.

    The manifest ``version`` field (e.g. ``"18.0.1.0.0"``) starts with the
    Odoo series. Parsed safely with ``ast.literal_eval``; returns '' when
    the file is unparseable, lacks a version, or the series is unsupported.
    """
    try:
        data = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError):
        return ''
    if not isinstance(data, dict):
        return ''
    parts = str(data.get('version', '')).split('.')
    if len(parts) >= 2:
        candidate = f'{parts[0]}.{parts[1]}'
        if candidate in _SUPPORTED_VERSIONS:
            return candidate
    return ''


def _detect_version_from_disk(root_dir, skip_paths=()):
    """Odoo series from the project's OWN module manifests (files on disk).

    A project is a single Odoo series, so the authoritative signal is the
    version declared by its own addons — never a submodule's ``branch=``
    label. Prefers top-level modules (where own addons live); only then
    descends, pruning vendored submodule trees in ``skip_paths``.
    """
    skip_abs = {os.path.join(root_dir, p) for p in skip_paths if p}
    # First pass: direct children — the project's own modules sit here.
    with suppress(OSError):
        for name in sorted(os.listdir(root_dir)):
            mpath = os.path.join(root_dir, name, '__manifest__.py')
            if os.path.isfile(mpath):
                with suppress(OSError):
                    ver = _version_from_manifest_text(Path(mpath).read_text())
                    if ver:
                        return ver
    # Second pass: deeper scan, skipping .git and submodule trees.
    for cur, dirs, files in os.walk(root_dir):
        dirs[:] = [
            d for d in dirs
            if d != '.git' and os.path.join(cur, d) not in skip_abs
        ]
        if '__manifest__.py' in files:
            with suppress(OSError):
                ver = _version_from_manifest_text(
                    Path(os.path.join(cur, '__manifest__.py')).read_text()
                )
                if ver:
                    return ver
    return ''


def _detect_version_from_git(tmpdir, skip_paths=()):
    """Odoo series from own module manifests via ``git show`` (no checkout).

    The preview clones with ``--no-checkout``; read the top-level tree and
    fetch each candidate ``<module>/__manifest__.py`` blob on demand (cheap
    with a blob-less clone). ``skip_paths`` excludes submodule containers.
    """
    skip = {p for p in skip_paths if p}
    skip |= {p.split('/')[0] for p in skip}
    try:
        ls = subprocess.run(
            ['git', '-C', tmpdir, 'ls-tree', '--name-only', 'HEAD'],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.SubprocessError:
        return ''
    for name in ls.stdout.splitlines():
        name = name.strip().rstrip('/')
        if not name or name in skip:
            continue
        try:
            show = subprocess.run(
                ['git', '-C', tmpdir, 'show', f'HEAD:{name}/__manifest__.py'],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.SubprocessError:
            continue
        if show.returncode == 0:
            ver = _version_from_manifest_text(show.stdout)
            if ver:
                return ver
    return ''


def _branch_for_sha(sub_url, sha, prefer, tokens=()):
    """Return the branch the pinned ``sha`` actually lives on, or ''.

    A ``.gitmodules`` ``branch=`` label can be stale (left behind after a
    version migration); the authoritative branch is the one whose history
    contains the pinned commit. Clones the remote blob-less (commits/trees
    only) and asks git which remote branches contain it, preferring
    ``prefer`` (the project Odoo version) when several do.

    The host is allow-listed before cloning: the URL ultimately comes from
    an attacker-controllable ``.gitmodules`` (SSRF / supply-chain defense).
    """
    if not sha:
        return ''
    https = _to_https(sub_url)
    host = (urlparse(https).hostname or '').lower()
    if host not in _ALLOWED_GIT_HOSTS or not https.startswith('https://'):
        return ''
    candidates = list(dict.fromkeys([t for t in tokens if t] + [None]))
    for token in candidates:
        clone_url = (
            f'https://x-access-token:{token}@{https[len("https://"):]}'
            if token else https
        )
        tmp = tempfile.mkdtemp(prefix='ic_subres_')
        try:
            res = subprocess.run(
                ['git', 'clone', '--filter=blob:none', '--no-checkout',
                 '--quiet', clone_url, tmp],
                capture_output=True, timeout=_SUBRES_CLONE_TIMEOUT,
            )
            if res.returncode != 0:
                continue
            cont = subprocess.run(
                ['git', '-C', tmp, 'branch', '-r', '--contains', sha],
                capture_output=True, text=True, timeout=15,
            )
            branches = []
            for line in cont.stdout.splitlines():
                name = line.strip()
                if not name or '->' in name:  # skip "origin/HEAD -> ..."
                    continue
                name = name.removeprefix('origin/')
                branches.append(name)
            if not branches:
                return ''
            if prefer and prefer in branches:
                return prefer
            return branches[0]
        except subprocess.SubprocessError:
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return ''


def _normalize_sub_branch(declared, project_version, sub_url, sha, tokens=()):
    """Decide the branch to record for a submodule.

    Odoo.sh defines a submodule by *repo + branch* (the pinned commit is a
    consequence) and a project cannot mix Odoo series, so:

    * empty / '.'                         → the project version ('main' if
                                            unknown);
    * version-like label != project       → stale; resolve the branch the
                                            pinned SHA truly lives on
                                            (preferring the project
                                            version), else the project
                                            version;
    * otherwise (matches project, or a
      non-version name like 'main')        → keep what the repo declares.
    """
    declared = (declared or '').strip()
    if not declared or declared == '.':
        return project_version or 'main'
    if (re.fullmatch(r'\d+\.\d', declared) and project_version
            and declared != project_version):
        return (
            _branch_for_sha(sub_url, sha, project_version, tokens)
            or project_version
        )
    return declared


class GitHubMixin:
    """GitHub App endpoints, repo introspection, project import."""

    # ── GitHub App endpoints ─────────────────────────────────────────────────

    @http.route(['/cloud/get_github_app'], type='json', auth='user')
    def cloud_get_github_app(self):
        app = request.env['cloud.github.app'].sudo().search([], limit=1)
        if not app:
            return {
                'configured': False,
                'app_id': '',
                'installation_id': '',
                'has_webhook_secret': False,
                'has_private_key': False,
                'has_pat': _has_pat(request.env),
            }
        slug = app.slug or ''
        return {
            'configured': True,
            'app_id': app.app_id or '',
            'installation_id': app.installation_id or '',
            'has_webhook_secret': _has_encrypted(app, 'webhook_secret'),
            'has_private_key': bool(app.sudo().private_key),
            'has_pat': _has_pat(request.env),
            'slug': slug,
            'install_url': f'https://github.com/apps/{slug}/installations/new' if slug else '',
        }

    @http.route(['/cloud/save_github_app'], type='json', auth='user')
    def cloud_save_github_app(self, vals):
        self._sec()._check_can_manage_settings()
        App = request.env['cloud.github.app'].sudo()
        app_id = (vals.get('app_id') or '').strip()
        installation_id = (vals.get('installation_id') or '').strip()
        private_key = (vals.get('private_key') or '').strip()
        webhook_secret = (vals.get('webhook_secret') or '').strip()
        force = bool(vals.get('force'))

        if not app_id:
            return {'ok': False, 'error': _('App ID is required.')}

        github_pat = (vals.get('github_pat') or '').strip()

        write_vals = {
            'app_id': app_id,
            'installation_id': installation_id,
        }
        if webhook_secret:
            write_vals['webhook_secret'] = webhook_secret
        if private_key:
            write_vals['private_key'] = private_key

        app = App.search([], limit=1)
        sensitive_changed = []
        if app:
            destructive_fields = []
            if private_key:
                destructive_fields.append('private_key')
            if webhook_secret:
                destructive_fields.append('webhook_secret')
            if destructive_fields and not force:
                return {
                    'ok': False,
                    'error': _(
                        "Overwriting %s on an existing GitHub App requires "
                        "explicit confirmation. Resend with force=true."
                    ) % ', '.join(destructive_fields),
                    'requires_force': destructive_fields,
                }
            sensitive_changed.extend(destructive_fields)
            if app_id != (app.app_id or ''):
                sensitive_changed.append('app_id')
            if installation_id != (app.installation_id or ''):
                sensitive_changed.append('installation_id')
            app.write(write_vals)
        else:
            if not private_key:
                return {
                    'ok': False,
                    'error': _('Private key is required '
                               'for the initial setup.'),
                }
            App.create(write_vals)
            sensitive_changed.append('initial_setup')

        # PAT stored in cloud.settings (EncryptedChar)
        if github_pat:
            request.env['cloud.settings']._get().write(
                {'github_pat': github_pat},
            )
            sensitive_changed.append('github_pat')

        if sensitive_changed:
            request.env['cloud.audit.log'].sudo().create({
                'action': 'GitHub App credentials updated',
                'details': ', '.join(sensitive_changed),
            })

        return {'ok': True}

    @http.route(['/cloud/save_github_pat'], type='json', auth='user')
    def cloud_save_github_pat(self, pat):
        """Save PAT independently of the GitHub App."""
        self._sec()._check_can_manage_settings()
        pat = (pat or '').strip()
        if not pat:
            return {'ok': False, 'error': _('PAT is empty.')}
        request.env['cloud.settings']._get().write({'github_pat': pat})
        return {'ok': True}

    @http.route(['/cloud/test_github_connection'], type='json', auth='user')
    def cloud_test_github_connection(self):
        self._sec()._check_can_manage_settings()
        return request.env['cloud.github.credential.service'].sudo().test_connection()

    @http.route(['/cloud/reset_github_app'], type='json', auth='user')
    def cloud_reset_github_app(self):
        """Delete the GitHub App configuration, allowing a fresh setup."""
        self._sec()._check_can_manage_settings()
        app = request.env['cloud.github.app'].sudo().search([], limit=1)
        if app:
            app.unlink()
        return {'ok': True}

    @http.route(['/cloud/detect_github_installation'], type='json', auth='user')
    def cloud_detect_github_installation(self):
        """List active installations via JWT and auto-update installation_id if exactly one."""
        self._sec()._check_can_manage_settings()
        try:
            svc = request.env['cloud.github.credential.service'].sudo()
            creds = svc.get_credentials()
            client = GitHubAppClient(creds)
            installations = client.list_installations()
        except Exception as exc:
            return safe_error_response(
                exc, _("Failed to detect GitHub installation"),
            )

        if not installations:
            return {'ok': False, 'error': _('No active installations found for this GitHub App. Install the app on your organization first.')}

        # Auto-update if there's exactly one installation
        if len(installations) == 1:
            inst_id = installations[0]['id']
            app = request.env['cloud.github.app'].sudo().search([], limit=1)
            if app:
                app.write({'installation_id': inst_id})
            return {
                'ok': True,
                'installation_id': inst_id,
                'account': installations[0]['account_login'],
                'auto_updated': True,
            }

        # Multiple installations — return them so the user can choose
        return {
            'ok': True,
            'installations': installations,
            'auto_updated': False,
        }
    # ── GitHub repo introspection ─────────────────────────────────────────────

    @http.route(['/cloud/get_repo_branches'], type='json', auth='user')
    def cloud_get_repo_branches(self, url):
        """Return branch names for a GitHub repo. Tries App first, then PAT."""
        self._sec()._check_cloud_group('group_cloud_consultant')

        def _fetch(client):
            owner, repo = _parse_github_repo_path(url)
            branches, page = [], 1
            while True:
                page_data = client.get(
                    f'/repos/{_gh_seg(owner)}/{_gh_seg(repo)}'
                    f'/branches?per_page=100&page={page}'
                ) or []
                branches.extend(b['name'] for b in page_data)
                if len(page_data) < 100:
                    break
                page += 1
            return branches

        svc = request.env['cloud.github.credential.service'].sudo()
        app_status = 'not_configured'  # not_configured | no_access | error
        pat_status = 'not_configured'

        # 1. Try GitHub App first (has org-level access)
        try:
            creds = svc.get_credentials()
            branches = _fetch(GitHubAppClient(creds))
            return {'ok': True, 'branches': branches}
        except GitHubAPIError as exc:
            _logger.info("[branches] App %s: %s", exc.status_code, exc)
            app_status = 'no_access'
            if exc.status_code not in (404, 401, 403):
                app_status = 'error'
        except (ValueError, UserError):
            _logger.debug("[branches] App not configured", exc_info=True)
            # app_status stays not_configured
        except Exception:
            _logger.exception("[branches] App unexpected error")
            app_status = 'error'

        # 2. Fall back to PAT
        pat = svc.get_pat()
        if pat:
            try:
                branches = _fetch(GitHubPATClient(pat))
                return {'ok': True, 'branches': branches}
            except GitHubAPIError as exc:
                _logger.info("[branches] PAT %s: %s", exc.status_code, exc)
                pat_status = 'no_access'
            except Exception:
                _logger.exception("[branches] PAT unexpected error")
                pat_status = 'error'

        # Build contextual error message
        msg = self._build_github_error(
            app_status, pat_status, bool(pat),
        )
        # ``settings_hint`` tells the frontend this error is fixable in the
        # global GitHub settings, so it can render a direct link there (the
        # message itself no longer points at an ambiguous "Settings" menu).
        return {'ok': False, 'error': msg, 'branches': [], 'settings_hint': True}

    @staticmethod
    def _build_github_error(app_status, pat_status, has_pat):
        """Build a user-friendly error based on what was tried."""
        if app_status == 'not_configured' and not has_pat:
            return _(
                "No GitHub credentials configured. "
                "Set up a GitHub App or a Personal Access Token (PAT) "
                "to access private repositories."
            )
        if app_status == 'not_configured' and pat_status == 'no_access':
            return _(
                "The PAT does not have access to this repository. "
                "Verify the token has the 'repo' scope, "
                "or set up a GitHub App."
            )
        if app_status == 'no_access' and not has_pat:
            return _(
                "The GitHub App does not have access to this repository. "
                "Install the App on the repository's organization, "
                "or configure a PAT."
            )
        if app_status == pat_status == 'no_access':
            return _(
                "Neither the GitHub App nor the PAT have access "
                "to this repository. Check that the App is "
                "installed on the correct organization and that "
                "the PAT has the 'repo' scope."
            )
        return _(
            "Could not access the repository. Check the URL "
            "and your GitHub credentials."
        )

    @http.route(['/cloud/get_branch_head'], type='json', auth='user')
    def cloud_get_branch_head(self, url, branch):
        """Return the HEAD commit SHA of a branch (for freeze)."""
        self._sec()._check_cloud_group('group_cloud_consultant')

        def _fetch(client):
            owner, repo = _parse_github_repo_path(url)
            data = client.get(
                f'/repos/{_gh_seg(owner)}/{_gh_seg(repo)}'
                f'/branches/{_gh_seg(branch)}'
            )
            return data['commit']['sha']

        svc = request.env['cloud.github.credential.service'].sudo()

        # 1. Try GitHub App first
        try:
            sha = _fetch(
                GitHubAppClient(svc.get_credentials())
            )
            return {'ok': True, 'sha': sha}
        except GitHubAPIError as exc:
            if exc.status_code not in (404, 401, 403):
                return safe_error_response(
                    exc, _("Failed to fetch branch head"),
                )
        except (ValueError, UserError):
            _logger.debug("[branch_head] App not configured", exc_info=True)
        except Exception:
            _logger.debug("[branch_head] App unexpected error", exc_info=True)

        # 2. Fall back to PAT
        try:
            pat = svc.get_pat()
            if pat:
                return {
                    'ok': True,
                    'sha': _fetch(GitHubPATClient(pat)),
                }
        except (GitHubAPIError, ValueError, UserError) as exc:
            return safe_error_response(
                exc, _("Failed to fetch branch head"),
            )
        except Exception:
            _logger.exception(
                "Error fetching branch HEAD for %s@%s",
                url, branch,
            )

        return {'ok': False, 'error': _('Unexpected error')}

    @http.route(['/cloud/freeze_repo'], type='json', auth='user')
    def cloud_freeze_repo(self, repo_id, model):
        """Freeze a repo to its current branch HEAD. Writes commit_sha to DB."""
        self._sec()._check_cloud_group('group_cloud_consultant')
        if model not in ('cloud.project.repo', 'cloud.instance.repo'):
            return {'ok': False, 'error': _('Invalid model')}
        repo = request.env[model].browse(repo_id)
        if not repo.exists():
            return {'ok': False, 'error': _('Repo not found')}
        res = self.cloud_get_branch_head(repo.url, repo.branch)
        if not res.get('ok'):
            return res
        repo.write({'commit_sha': res['sha']})
        return {'ok': True, 'sha': res['sha']}

    @http.route(['/cloud/unfreeze_repo'], type='json', auth='user')
    def cloud_unfreeze_repo(self, repo_id, model):
        """Unfreeze a repo — clears commit_sha so rebuilds use branch tip."""
        self._sec()._check_cloud_group('group_cloud_consultant')
        if model not in ('cloud.project.repo', 'cloud.instance.repo'):
            return {'ok': False, 'error': _('Invalid model')}
        repo = request.env[model].browse(repo_id)
        if not repo.exists():
            return {'ok': False, 'error': _('Repo not found')}
        repo.write({'commit_sha': False})
        return {'ok': True}

    @http.route(['/cloud/freeze_all_repos'], type='json', auth='user')
    def cloud_freeze_all_repos(self, repo_ids, model):
        """Freeze multiple repos at once."""
        self._sec()._check_cloud_group('group_cloud_consultant')
        if model not in ('cloud.project.repo', 'cloud.instance.repo'):
            return {'ok': False, 'error': _('Invalid model')}
        # search([('id', 'in', repo_ids)]) applies record rules and
        # silently filters out rows the caller has no access to,
        # avoiding a leak of project-internal SHAs to a non-member
        # who passed foreign repo_ids.
        repos = request.env[model].search([('id', 'in', repo_ids)])
        results = {}
        for repo in repos:
            if not repo.url or not repo.branch or repo.commit_sha:
                continue
            res = self.cloud_get_branch_head(repo.url, repo.branch)
            if res.get('ok'):
                repo.write({'commit_sha': res['sha']})
                results[repo.id] = res['sha']
        return {'ok': True, 'results': results}

    @http.route(['/cloud/unfreeze_all_repos'], type='json', auth='user')
    def cloud_unfreeze_all_repos(self, repo_ids, model):
        """Unfreeze multiple repos at once."""
        self._sec()._check_cloud_group('group_cloud_consultant')
        if model not in ('cloud.project.repo', 'cloud.instance.repo'):
            return {'ok': False, 'error': _('Invalid model')}
        # Same record-rule scoping as ``cloud_freeze_all_repos`` —
        # only unfreeze repos the caller can see.
        repos = request.env[model].search([('id', 'in', repo_ids)])
        repos.filtered('commit_sha').write({'commit_sha': False})
        return {'ok': True}

    @http.route(['/cloud/get_repo_modules'], type='json', auth='user')
    def cloud_get_repo_modules(self, url, branch):
        """Return Odoo addon module names found at the root of a branch."""
        self._sec()._check_cloud_group('group_cloud_consultant')

        def _fetch(client):
            owner, repo = _parse_github_repo_path(url)
            tree = client.get(
                f'/repos/{_gh_seg(owner)}/{_gh_seg(repo)}'
                f'/git/trees/{_gh_seg(branch)}?recursive=1'
            )
            return sorted({
                item['path'].split('/')[0]
                for item in tree.get('tree', [])
                if item['path'].count('/') == 1
                and item['path'].endswith('/__manifest__.py')
            })

        svc = request.env['cloud.github.credential.service'].sudo()

        # 1. Try PAT first if configured
        try:
            pat = svc.get_pat()
            if pat:
                return {'ok': True, 'modules': _fetch(GitHubPATClient(pat))}
        except GitHubAPIError as exc:
            if exc.status_code not in (404, 401):
                return safe_error_response(
                    exc, _("Failed to fetch repo modules"),
                    extra={'modules': []},
                )
        except (ValueError, UserError) as exc:
            return safe_error_response(
                exc, _("Failed to fetch repo modules"),
                extra={'modules': []},
            )
        except Exception:
            _logger.debug("[repo_modules] PAT unexpected error", exc_info=True)

        # 2. Fall back to GitHub App token
        try:
            return {'ok': True, 'modules': _fetch(GitHubAppClient(svc.get_credentials()))}
        except (ValueError, UserError, GitHubAPIError) as exc:
            return safe_error_response(
                exc, _("Failed to fetch repo modules"),
                extra={'modules': []},
            )
        except Exception as exc:
            _logger.exception("Error fetching modules for %s@%s", url, branch)
            return safe_error_response(
                exc, _("Failed to fetch repo modules"),
                extra={'modules': []},
            )

    @http.route(['/cloud/get_repo_requirements'], type='json', auth='user')
    def cloud_get_repo_requirements(self, url, branch):
        """Fetch requirements.txt from the root of a GitHub repo branch."""
        self._sec()._check_cloud_group('group_cloud_consultant')

        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError as exc:
            return safe_error_response(
                exc, _("Invalid repository URL"),
                extra={'content': '', 'found': False},
            )

        def _fetch(client):
            data = client.get(
                f'/repos/{_gh_seg(owner)}/{_gh_seg(repo_name)}'
                f'/contents/requirements.txt?ref={_gh_seg(branch)}'
            )
            return base64.b64decode(
                data.get('content', '').replace('\n', '').replace(' ', '')
            ).decode('utf-8')

        svc = request.env['cloud.github.credential.service'].sudo()

        # 1. Try PAT first
        try:
            pat = svc.get_pat()
            if pat:
                content = _fetch(GitHubPATClient(pat))
                return {'ok': True, 'found': True, 'content': content}
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return {'ok': True, 'found': False, 'content': ''}
            if exc.status_code != 401:
                return safe_error_response(
                    exc, _("Failed to fetch requirements.txt"),
                    extra={'content': '', 'found': False},
                )
        except (ValueError, UserError):
            _logger.debug("[repo_requirements] PAT not configured", exc_info=True)
        except Exception:
            _logger.debug("[repo_requirements] PAT unexpected error", exc_info=True)

        # 2. Try GitHub App token
        try:
            content = _fetch(GitHubAppClient(svc.get_credentials()))
            return {'ok': True, 'found': True, 'content': content}
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return {'ok': True, 'found': False, 'content': ''}
            return safe_error_response(
                exc, _("Failed to fetch requirements.txt"),
                extra={'content': '', 'found': False},
            )
        except (ValueError, UserError):
            _logger.debug("[repo_requirements] App not configured", exc_info=True)
        except Exception:
            _logger.debug("[repo_requirements] App unexpected error", exc_info=True)

        # 3. Unauthenticated fallback (public repos)
        try:
            api_url = (
                f'https://api.github.com/repos/{_gh_seg(owner)}'
                f'/{_gh_seg(repo_name)}'
                f'/contents/requirements.txt?ref={_gh_seg(branch)}'
            )
            req = urllib.request.Request(api_url, headers={
                'User-Agent': 'incubacloud/1.0',
                'Accept': 'application/vnd.github+json',
            })
            with safe_urlopen(req, timeout=10) as resp:  # nosec B310 — hardcoded https://api.github.com
                data = json.loads(resp.read().decode())
                raw = data.get('content', '').replace('\n', '').replace(' ', '')
                content = base64.b64decode(raw).decode('utf-8')
                return {'ok': True, 'found': True, 'content': content}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {'ok': True, 'found': False, 'content': ''}
        except Exception:
            _logger.debug("[repo_requirements] unauthenticated fallback error", exc_info=True)

        return {'ok': True, 'found': False, 'content': ''}
    # ── odoo.sh import wizard ────────────────────────────────────────────────

    @http.route(['/cloud/fetch_odoojs_submodules'], type='json', auth='user')
    def cloud_fetch_odoojs_submodules(self, repo_url, branch='main'):
        """Clone a GitHub repo (minimal) to discover submodules and pinned SHAs.

        Uses ``git clone --depth 1 --no-checkout --filter=blob:none`` to
        download the absolute minimum, then reads ``.gitmodules`` and
        submodule SHAs from the git tree objects directly.
        """
        self._sec()._check_cloud_group('group_cloud_consultant')

        _CLONE_TIMEOUT = 30  # seconds

        branch = (branch or 'main').strip()
        if not re.fullmatch(r'[a-zA-Z0-9._/\-]+', branch) or branch.startswith('-'):
            return {'ok': False, 'error': _('Invalid branch name')}

        try:
            _owner, repo_name = _parse_github_repo_path(repo_url)
        except ValueError as exc:
            return safe_error_response(exc, _("Invalid repository URL"))

        # Detect Odoo version early from the branch name (e.g. branch
        # '18.0'). When the branch is not version-like (e.g. 'main') the
        # version is determined later from the project's own manifests.
        _ver_match = re.fullmatch(r'(\d+\.\d)', branch)
        odoo_version = (
            _ver_match.group(1)
            if _ver_match and _ver_match.group(1) in _SUPPORTED_VERSIONS
            else ''
        )

        # ── Helpers ────────────────────────────────────────────────────────────

        def _resolve_url(sub_url):
            """Resolve a potentially relative submodule URL to absolute HTTPS.

            Rejects submodules pointing to non-github hosts: a
            ``.gitmodules`` file is attacker-controlled data; without
            this check a crafted submodule URL would let git clone
            fetch from an arbitrary host (SSRF / supply-chain).
            """
            sub_url = sub_url.strip()
            if sub_url.startswith(('https://', 'http://', 'git@', 'ssh://')):
                absolute = sub_url
            else:
                parsed = urlparse(repo_url.rstrip('/'))
                if parsed.path.endswith('.git'):
                    parsed = parsed._replace(path=parsed.path[:-4])
                new_path = posixpath.normpath(
                    posixpath.join(parsed.path, sub_url)
                )
                absolute = urlunparse(parsed._replace(
                    path=new_path, query='', fragment='',
                ))
            # Validate the host in the resolved URL. SCP-style
            # ``git@host:path`` has no scheme, so urlparse misclassifies
            # it; extract the host segment manually.
            if absolute.startswith('git@') and ':' in absolute:
                host_part = absolute.split('@', 1)[1].split(':', 1)[0]
            else:
                host_part = (urlparse(absolute).hostname or '')
            if host_part.lower() not in _ALLOWED_GIT_HOSTS:
                raise ValueError(
                    f"Refusing submodule from non-github host: {host_part!r}"
                )
            return absolute

        def _parse_gitmodules(content):
            submodules = []
            current = {}
            for line in content.splitlines():
                line = line.strip()
                if line.startswith('[submodule'):
                    if current:
                        submodules.append(current)
                    current = {}
                elif '=' in line:
                    key, _, val = line.partition('=')
                    current[key.strip()] = val.strip()
            if current:
                submodules.append(current)
            # Return raw declared branches; the project version is decided
            # from the project's own manifests and each submodule branch is
            # normalised later (see _clone_and_extract / the normalisation
            # pass below). A submodule ``branch=`` label is never trusted to
            # determine the project's Odoo version.
            result = []
            for s in submodules:
                if 'url' not in s:
                    continue
                result.append({
                    'url': _resolve_url(s['url']),
                    'branch': s.get('branch', '').strip(),
                    'path': s.get('path', ''),
                })
            return result

        def _build_clone_url(token=None):
            """Build HTTPS clone URL, optionally with auth token.

            Refuses non-github hosts: ``urlparse(url).hostname`` is the
            value the attacker would control if ``repo_url`` were
            crafted to start with a different scheme/host.
            """
            url = repo_url.strip()
            if not url.startswith(('https://', 'http://')):
                url = f'https://{url}'
            parsed = urlparse(url)
            host = (parsed.hostname or '').lower()
            if host not in _ALLOWED_GIT_HOSTS:
                raise ValueError(
                    f"Refusing to clone from non-github host: {host!r}"
                )
            path = parsed.path.rstrip('/')
            if not path.endswith('.git'):
                path += '.git'
            if token:
                return f'https://x-access-token:{token}@{host}{path}'
            return f'https://{host}{path}'

        def _clone_and_extract(clone_url):
            """Shallow-clone, extract .gitmodules + submodule SHAs + HEAD SHA.

            Returns (submodules, main_sha, warning) or raises on fatal error.
            """
            tmpdir = tempfile.mkdtemp(prefix='ic_import_')
            try:
                subprocess.run(
                    [
                        'git', 'clone',
                        '--depth', '1',
                        '--no-checkout',
                        '--filter=blob:none',
                        '--branch', branch,
                        clone_url,
                        tmpdir,
                    ],
                    capture_output=True, timeout=_CLONE_TIMEOUT,
                    check=True,
                )

                # HEAD SHA
                main_sha = subprocess.run(
                    ['git', '-C', tmpdir, 'rev-parse', 'HEAD'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()

                # .gitmodules content
                gm_result = subprocess.run(
                    ['git', '-C', tmpdir, 'show', 'HEAD:.gitmodules'],
                    capture_output=True, text=True, timeout=5,
                )
                if gm_result.returncode != 0:
                    return [], main_sha, None  # No submodules

                submodules = _parse_gitmodules(gm_result.stdout)

                # Submodule SHAs from ls-tree (type=commit entries)
                ls_result = subprocess.run(
                    ['git', '-C', tmpdir, 'ls-tree', '-r', 'HEAD'],
                    capture_output=True, text=True, timeout=5,
                )
                # ls-tree output: "<mode> <type> <sha>\t<path>"
                sha_by_path = {}
                for line in ls_result.stdout.splitlines():
                    parts = line.split(None, 3)
                    if len(parts) == 4 and parts[1] == 'commit':
                        # parts[3] has the path after the tab
                        sha_by_path[parts[3].strip()] = parts[2]

                for sub in submodules:
                    path = sub.get('path', '')
                    if path in sha_by_path:
                        sub['commit_sha'] = sha_by_path[path]

                # A project is a single Odoo series: decide it from the
                # project's own manifests (not submodule branch labels).
                nonlocal odoo_version
                if not odoo_version:
                    odoo_version = _detect_version_from_git(
                        tmpdir,
                        skip_paths=[s.get('path', '') for s in submodules],
                    )

                return submodules, main_sha, None

            except subprocess.TimeoutExpired:
                _logger.warning("Git clone timed out for %s", repo_url)
                return None, '', (
                    'Repository clone timed out. '
                    'Repositories were imported without pinned commits — '
                    'you can freeze them manually from Settings.'
                )
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b'').decode(errors='replace')
                stdout = (exc.stdout or b'').decode(errors='replace')
                _logger.warning(
                    "Git clone failed for %s (exit %s): "
                    "stderr=%s stdout=%s",
                    repo_url, exc.returncode,
                    stderr[:500], stdout[:200],
                )
                raise
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        def _api_fallback_gitmodules():
            """Fallback: fetch .gitmodules via GitHub API (no SHAs).

            Uses App token or PAT for private repos.
            """

            endpoint = (
                f'/repos/{_owner}/{repo_name}'
                f'/contents/.gitmodules?ref={branch}'
            )

            # Try App first, then PAT
            clients = []
            with suppress(Exception):
                clients.append(
                    GitHubAppClient(svc.get_credentials())
                )
            pat = svc.get_pat()
            if pat:
                clients.append(GitHubPATClient(pat))

            for client in clients:
                try:
                    data = client.get(endpoint)
                    raw = (
                        data.get('content', '')
                        .replace('\n', '')
                        .replace(' ', '')
                    )
                    content = base64.b64decode(raw).decode()
                    return _parse_gitmodules(content)
                except Exception:
                    continue
            return []

        # ── Build token list (App first, PAT second) ─────────────

        svc = request.env['cloud.github.credential.service'].sudo()
        tokens = []
        with suppress(Exception):
            client = GitHubAppClient(svc.get_credentials())
            tokens.append(client.get_installation_token())
        with suppress(Exception):
            pat = svc.get_pat()
            if pat:
                tokens.append(pat)
        if not tokens:
            tokens.append(None)  # unauthenticated attempt

        # ── Clone and extract (try each token) ────────────────────

        sha_warning = None
        submodules = None
        main_sha = ''

        for token in tokens:
            clone_url = _build_clone_url(token)
            try:
                result = _clone_and_extract(clone_url)
                submodules, main_sha, warning = result
                if submodules is None:
                    # Timeout — continue to next token
                    sha_warning = warning
                    continue
                sha_warning = None
                break  # success
            except Exception:
                _logger.info(
                    "[import] Clone failed with token "
                    "(len=%s), trying next",
                    len(token) if token else 0,
                )
                continue

        # All clones failed — API fallback (no SHAs)
        if submodules is None:
            if not sha_warning:
                sha_warning = (
                    'Could not clone repository to read '
                    'pinned commits. Repositories were imported '
                    'without pinned commits — you can freeze '
                    'them manually from Settings.'
                )
            try:
                submodules = _api_fallback_gitmodules()
            except Exception:
                _logger.exception(
                    "Error fetching submodules for %s",
                    repo_url,
                )
                return {
                    'ok': False,
                    'error': _(
                        'Could not fetch repository data. '
                        'Check the URL and configure GitHub '
                        'credentials for private repos.'
                    ),
                }
            main_sha = ''

        # Record repo + branch for each submodule (Odoo.sh's own model);
        # a stale version-like label is replaced by the branch the pinned
        # SHA truly lives on. The commit stays pinned in 'commit_sha'.
        for sub in submodules or []:
            sub['branch'] = _normalize_sub_branch(
                sub.get('branch', ''), odoo_version,
                sub.get('url', ''), sub.get('commit_sha', ''), tokens,
            )

        result = {
            'ok': True,
            'repo_name': repo_name,
            'submodules': submodules,
            'odoo_version': odoo_version,
            'main_commit_sha': main_sha,
        }
        if sha_warning:
            result['sha_warning'] = sha_warning
        return result

    # ── Project import (unified: doodba / odoo.sh / simple) ─────────────────

    @http.route(['/cloud/import_project'], type='json', auth='user')
    def cloud_import_project(self, url, branch='main'):
        """Import a GitHub repo as project + production instance.

        Detects repo type (doodba / odoo.sh / simple), parses config,
        creates project with repos, then creates a production instance
        inheriting project defaults + type-specific config.
        """

        self._sec()._check_can_create_instance()

        branch = (branch or 'main').strip()
        if not re.fullmatch(r'[a-zA-Z0-9._/\-]+', branch) or branch.startswith('-'):
            return {'ok': False, 'error': _('Invalid branch name')}

        _CLONE_TIMEOUT = 45
        _SSH_RE = re.compile(r'^git@[^:]+:(.+?)(?:\.git)?$')

        def _ssh_to_https(u):
            """Convert SSH git URL to HTTPS."""
            u = (u or '').strip()
            m = _SSH_RE.match(u)
            if m:
                return f'https://github.com/{m.group(1)}.git'
            return u

        def _parse_repos_yaml(content, odoo_version):
            """Parse git-aggregator repos.yaml into repo dicts."""
            data = yaml.safe_load(content) or {}
            repos = []
            for alias, cfg in data.items():
                if alias in ('./odoo', 'odoo'):
                    continue  # core handled via odoo_version
                if not isinstance(cfg, dict):
                    continue
                remotes = cfg.get('remotes', {})
                repo_url = next(iter(remotes.values()), '') if remotes else ''
                repo_url = _ssh_to_https(repo_url)
                # Branch from target or first merge
                branch_val = ''
                target = cfg.get('target', '')
                if target:
                    parts = target.split()
                    branch_val = parts[-1] if len(parts) >= 2 else parts[0]
                if not branch_val:
                    merges = cfg.get('merges', [])
                    if merges and isinstance(merges[0], str):
                        parts = merges[0].split()
                        branch_val = parts[-1] if len(parts) >= 2 else parts[0]
                # Resolve $ODOO_VERSION
                if '$ODOO_VERSION' in branch_val and odoo_version:
                    branch_val = branch_val.replace(
                        '$ODOO_VERSION', odoo_version,
                    )
                repos.append({
                    'url': repo_url,
                    'branch': branch_val or 'main',
                    'alias': alias,
                })
            return repos

        def _parse_addons_yaml(content):
            """Parse addons.yaml into {alias: addons_str} dict."""
            data = yaml.safe_load(content) or {}
            result = {}
            for alias, addons in data.items():
                if isinstance(addons, list):
                    if addons == ['*'] or '*' in addons:
                        result[alias] = ''  # all addons
                    else:
                        result[alias] = ','.join(str(a) for a in addons)
                elif addons == '*':
                    result[alias] = ''
                else:
                    result[alias] = str(addons) if addons else ''
            return result

        def _parse_copier_answers(content):
            """Parse .copier-answers.yml, return relevant fields."""
            data = yaml.safe_load(content) or {}
            domains = []
            for d in data.get('domains_prod', []) or []:
                if isinstance(d, dict):
                    for h in d.get('hosts', []):
                        domains.append({
                            'hostname': h,
                            'redirect_to': d.get('redirect_to', ''),
                        })
            ov = data.get('odoo_version', '')
            return {
                'odoo_version': f'{float(ov):.1f}' if ov else '',
                'project_author': data.get('project_author', ''),
                'project_license': data.get('project_license', ''),
                'project_name': data.get('project_name', ''),
                'postgres_version': str(data.get('postgres_version', '')),
                'postgres_dbname': data.get('postgres_dbname', 'prod'),
                'postgres_username': data.get('postgres_username', 'odoo'),
                'odoo_proxy': data.get('odoo_proxy', 'traefik'),
                'odoo_initial_lang': data.get('odoo_initial_lang', ''),
                'smtp_relay_host': data.get('smtp_relay_host', ''),
                'smtp_relay_port': data.get('smtp_relay_port', 587),
                'smtp_relay_user': data.get('smtp_relay_user', ''),
                'domains': domains,
            }

        # ── Parse repo URL ────────────────────────────────────────────
        try:
            _owner, repo_name = _parse_github_repo_path(url)
        except ValueError as exc:
            return safe_error_response(exc, _("Invalid repository URL"))

        # ── Build clone URL with auth ─────────────────────────────────
        svc = request.env['cloud.github.credential.service'].sudo()
        tokens = []
        with suppress(Exception):
            client = GitHubAppClient(svc.get_credentials())
            tokens.append(client.get_installation_token())
        with suppress(Exception):
            pat = svc.get_pat()
            if pat:
                tokens.append(pat)
        if not tokens:
            tokens.append(None)

        def _build_clone_url(token):
            u = url.strip()
            if not u.startswith(('https://', 'http://')):
                m = _SSH_RE.match(u)
                u = (
                    f'https://github.com/{m.group(1)}.git'
                    if m else f'https://{u}'
                )
            parsed = urlparse(u)
            host = (parsed.hostname or '').lower()
            if host not in _ALLOWED_GIT_HOSTS:
                raise ValueError(
                    f"Refusing to clone from non-github host: {host!r}"
                )
            path = parsed.path.rstrip('/')
            if not path.endswith('.git'):
                path += '.git'
            if token:
                return f'https://x-access-token:{token}@{host}{path}'
            return f'https://{host}{path}'

        # ── Clone and detect type ─────────────────────────────────────
        repo_type = 'simple'
        repos_data = []
        addons_map = {}
        copier = {}
        pip_deps = ''
        apt_deps = ''
        odoo_conf = ''
        odoo_version = ''
        submodules = []

        last_error = None
        for token in tokens:
            clone_url = _build_clone_url(token)
            tmpdir = tempfile.mkdtemp(prefix='ic_import_')
            try:
                subprocess.run(
                    [
                        'git', 'clone', '--depth', '1',
                        '--branch', branch,
                        clone_url, tmpdir,
                    ],
                    capture_output=True, timeout=_CLONE_TIMEOUT, check=True,
                )

                # Detect type
                repos_yaml_path = os.path.join(
                    tmpdir, 'odoo', 'custom', 'src', 'repos.yaml',
                )
                gitmodules_path = os.path.join(tmpdir, '.gitmodules')

                if os.path.isfile(repos_yaml_path):
                    repo_type = 'doodba'

                    # Copier answers
                    copier_path = os.path.join(
                        tmpdir, '.copier-answers.yml',
                    )
                    if os.path.isfile(copier_path):
                        with open(copier_path) as f:
                            copier = _parse_copier_answers(f.read())
                        odoo_version = copier.get('odoo_version', '')

                    # Detect odoo_version from branch if not in copier
                    if not odoo_version:
                        vm = re.fullmatch(r'(\d+\.\d)', branch)
                        odoo_version = vm.group(1) if vm else ''

                    # repos.yaml
                    with open(repos_yaml_path) as f:
                        repos_data = _parse_repos_yaml(
                            f.read(), odoo_version,
                        )

                    # addons.yaml
                    addons_path = os.path.join(
                        tmpdir, 'odoo', 'custom', 'src', 'addons.yaml',
                    )
                    if os.path.isfile(addons_path):
                        with open(addons_path) as f:
                            addons_map = _parse_addons_yaml(f.read())

                    # Apply addons to repos
                    for r in repos_data:
                        r['addons'] = addons_map.get(r['alias'], '')

                    # pip.txt
                    pip_path = os.path.join(
                        tmpdir, 'odoo', 'custom',
                        'dependencies', 'pip.txt',
                    )
                    if os.path.isfile(pip_path):
                        with open(pip_path) as f:
                            pip_deps = f.read().strip()

                    # apt.txt
                    apt_path = os.path.join(
                        tmpdir, 'odoo', 'custom',
                        'dependencies', 'apt.txt',
                    )
                    if os.path.isfile(apt_path):
                        with open(apt_path) as f:
                            apt_deps = f.read().strip()

                    # conf.d/*.conf
                    conf_dir = os.path.join(
                        tmpdir, 'odoo', 'custom', 'conf.d',
                    )
                    if os.path.isdir(conf_dir):
                        confs = sorted(
                            f for f in os.listdir(conf_dir)
                            if f.endswith('.conf')
                        )
                        parts = []
                        for cf in confs:
                            with open(os.path.join(conf_dir, cf)) as f:
                                parts.append(f.read().strip())
                        odoo_conf = '\n'.join(parts)

                elif os.path.isfile(gitmodules_path):
                    repo_type = 'odoosh'

                    # Detect odoo_version from branch (e.g. branch '18.0')
                    vm = re.fullmatch(r'(\d+\.\d)', branch)
                    odoo_version = (
                        vm.group(1)
                        if vm and vm.group(1) in _SUPPORTED_VERSIONS
                        else ''
                    )

                    # Add the main repo itself first
                    repos_data.append({
                        'url': _ssh_to_https(url),
                        'branch': branch,
                        'alias': repo_name,
                        'addons': '',
                    })

                    # Parse .gitmodules (line-by-line, tolerant of dupes)
                    gm_content = Path(gitmodules_path).read_text()
                    current = {}
                    gm_subs = []
                    for line in gm_content.splitlines():
                        line = line.strip()
                        if line.startswith('[submodule'):
                            if current:
                                gm_subs.append(current)
                            current = {}
                        elif '=' in line:
                            key, _sep, val = line.partition('=')
                            current[key.strip()] = val.strip()
                    if current:
                        gm_subs.append(current)
                    # Submodule SHAs from ls-tree
                    ls_result = subprocess.run(
                        ['git', '-C', tmpdir, 'ls-tree', '-r', 'HEAD'],
                        capture_output=True, text=True, timeout=5,
                    )
                    sha_by_path = {}
                    for line in ls_result.stdout.splitlines():
                        parts = line.split(None, 3)
                        if (
                            len(parts) == 4
                            and parts[1] == 'commit'
                        ):
                            sha_by_path[parts[3].strip()] = parts[2]

                    # A project is a single Odoo series: decide it from the
                    # project's own manifests, never submodule branch labels.
                    if not odoo_version:
                        odoo_version = _detect_version_from_disk(
                            tmpdir,
                            skip_paths=[s.get('path', '') for s in gm_subs],
                        )

                    for s in gm_subs:
                        sub_url = s.get('url', '')
                        if not sub_url:
                            continue
                        sub_url = _ssh_to_https(sub_url)
                        sub_path = s.get('path', '')
                        # Record repo + branch; a stale version-like label is
                        # replaced by the branch the pinned SHA truly lives on.
                        sub_branch = _normalize_sub_branch(
                            s.get('branch', ''), odoo_version, sub_url,
                            sha_by_path.get(sub_path, ''), tokens,
                        )
                        repos_data.append({
                            'url': sub_url,
                            'branch': sub_branch,
                            'alias': sub_path.split('/')[-1],
                            'addons': '',
                            'commit_sha': sha_by_path.get(
                                sub_path, '',
                            ),
                        })

                else:
                    repo_type = 'simple'
                    repos_data = [{
                        'url': _ssh_to_https(url),
                        'branch': branch,
                        'alias': repo_name,
                        'addons': '',
                    }]

                # Fallback (doodba / simple): detect from own manifests.
                if not odoo_version:
                    odoo_version = _detect_version_from_disk(tmpdir)

                last_error = None
                break  # success

            except subprocess.TimeoutExpired:
                last_error = _(
                    'Repository clone timed out. '
                    'Check the URL and try again.'
                )
            except subprocess.CalledProcessError as exc:
                last_error = _(
                    'Could not clone repository. '
                    'Check the URL and credentials.'
                )
                _logger.warning(
                    "Import clone failed: %s",
                    (exc.stderr or b'').decode(errors='replace')[:500],
                )
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        if last_error:
            return {'ok': False, 'error': last_error}

        # ── Create project ────────────────────────────────────────────
        project_name = copier.get('project_name') or repo_name
        proj_vals = {
            'name': project_name,
            'pip_dependencies': pip_deps or None,
            'apt_dependencies': apt_deps or None,
        }
        if odoo_version:
            proj_vals['odoo_version'] = odoo_version
        if copier.get('project_author'):
            proj_vals['project_author'] = copier['project_author']
        if copier.get('project_license'):
            proj_vals['project_license'] = copier['project_license']

        project = request.env['cloud.project'].create(proj_vals)

        # Create project repos.
        # For doodba: pip.txt is already resolved — skip fetching
        # requirements.txt from each repo to avoid false conflicts.
        # For odoosh/simple: fetch and merge requirements.txt from each repo.
        Repo = request.env['cloud.project.repo']
        if repo_type == 'doodba':
            Repo = Repo.with_context(skip_apply_requirements=True)
        for seq, r in enumerate(repos_data, start=10):
            if r.get('url'):
                Repo.create({
                    'project_id': project.id,
                    'sequence': seq,
                    'url': r['url'],
                    'branch': r.get('branch', 'main'),
                    'addons': r.get('addons', ''),
                    'excludes': r.get('excludes', ''),
                    'commit_sha': r.get('commit_sha', ''),
                })

        # ── Create production instance ────────────────────────────────
        inst_vals = {
            'name': 'production',
            'project_id': project.id,
            'environment': 'production',
        }
        if odoo_version:
            inst_vals['odoo_version'] = odoo_version

        if odoo_conf:
            inst_vals['odoo_conf'] = odoo_conf

        # Doodba-specific fields from copier-answers
        if repo_type == 'doodba' and copier:
            if copier.get('postgres_version'):
                inst_vals['postgres_version'] = copier['postgres_version']
            if copier.get('postgres_dbname'):
                inst_vals['postgres_dbname'] = copier['postgres_dbname']
            if copier.get('postgres_username'):
                inst_vals['postgres_username'] = copier['postgres_username']
            if copier.get('odoo_proxy'):
                inst_vals['odoo_proxy'] = copier['odoo_proxy']
            if copier.get('smtp_relay_host'):
                inst_vals['smtp_relay_host'] = copier['smtp_relay_host']
            if copier.get('smtp_relay_port'):
                inst_vals['smtp_relay_port'] = copier['smtp_relay_port']
            if copier.get('smtp_relay_user'):
                inst_vals['smtp_relay_user'] = copier['smtp_relay_user']
            domains = copier.get('domains', [])
            if domains:
                inst_vals['domain_ids'] = [
                    (0, 0, {
                        'hostname': d['hostname'],
                        'redirect_to': d.get('redirect_to', ''),
                    })
                    for d in domains if d.get('hostname')
                ]

        # Auto-assign host if enabled
        autoassign = (
            request.env['ir.config_parameter'].sudo()
            .get_param('incubacloud.host_autoassign', '0') == '1'
        )
        if autoassign:
            cpus, ram = (
                request.env['cloud.instance']
                ._compute_instance_resources(inst_vals)
            )
            best_host = (
                request.env['cloud.host'].select_best_host(cpus, ram)
            )
            if best_host:
                inst_vals['host_id'] = best_host.id

        instance = request.env['cloud.instance'].create(inst_vals)

        return {
            'ok': True,
            'project_id': project.id,
            'instance_id': instance.id,
            'project_name': project.name,
            'repo_type': repo_type,
            'repo_count': len(repos_data),
        }
