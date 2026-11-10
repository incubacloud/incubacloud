"""GitHub-related endpoints: App configuration, repo introspection and
project import from a GitHub repository.

Mixed into ``CloudDataLoadController`` in ``data_load.py``.
"""
import base64
import json
import logging
import urllib.error
import urllib.request
from contextlib import suppress

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

from ...github.client import (
    GitHubAnonymousClient,
    GitHubAppClient,
    GitHubAPIError,
    GitHubPATClient,
)
from ...github.http_utils import safe_urlopen
from ...github.import_inspector import (
    GitHubImportInspector,
    ImportInspectionError,
    parse_github_repo_url,
    to_https as _api_to_https,
    validate_branch,
    version_from_manifest_text as _api_version_from_manifest_text,
)
from ...models._concurrency import try_advisory_xact_lock
from ...models._repo_requirements import (
    GitReadBudget,
    apply_requirements_content,
    consume_git_read,
    consume_hourly_quota,
    git_source_addons,
    git_source_requirements,
    is_safe_git_ref,
    resolve_git_source,
    resolve_repo_ref,
)
from ...models.cloud_repo_mixin import (
    _url_host,
    may_read_with_panel_credentials,
)
from ...net.git_reader import GitReaderError
from ._helpers import (
    _gh_seg, _has_encrypted, _has_pat, _parse_github_repo_path,
    import_series_refusal,
)
from ._routes_git_providers import git_error_message
from .._safe_error import safe_error_response

_logger = logging.getLogger(__name__)

_GITHUB_IMPORT_LOCK_NAMESPACE = "incubacloud.github.import"


def _translated_inspection_error(error):
    """Translate the inspector's stable messages at request time."""
    messages = {
        'Invalid repository URL.': _('Invalid repository URL.'),
        'Invalid branch name.': _('Invalid branch name.'),
        'Repository declares too many submodules.': _(
            'Repository declares too many submodules.'
        ),
        'Invalid submodule URL.': _('Invalid submodule URL.'),
        'Invalid repos.yaml structure.': _('Invalid repos.yaml structure.'),
        'Repository declares too many repositories.': _(
            'Repository declares too many repositories.'
        ),
        'Invalid addons.yaml structure.': _('Invalid addons.yaml structure.'),
        'Repository declares too many addon aliases.': _(
            'Repository declares too many addon aliases.'
        ),
        'Invalid copier answers structure.': _(
            'Invalid copier answers structure.'
        ),
        'Invalid production domains configuration.': _(
            'Invalid production domains configuration.'
        ),
        'Could not access the repository. Check the URL and GitHub credentials.': _(
            'Could not access the repository. Check the URL and GitHub credentials.'
        ),
        'Could not access a repository declared by the project.': _(
            'Could not access a repository declared by the project.'
        ),
        'Could not parse repository configuration.': _(
            'Could not parse repository configuration.'
        ),
        'Invalid submodule path.': _('Invalid submodule path.'),
        'Repository declares too many config files.': _(
            'Repository declares too many config files.'
        ),
        'Repository contains too many manifests.': _(
            'Repository contains too many manifests.'
        ),
        'Repository configuration is not a regular file.': _(
            'Repository configuration is not a regular file.'
        ),
        'Repository configuration file is too large.': _(
            'Repository configuration file is too large.'
        ),
        'Repository requirements file is too large.': _(
            'Repository requirements file is too large.'
        ),
        'Unsupported repository content encoding.': _(
            'Unsupported repository content encoding.'
        ),
        'Repository content size is invalid.': _(
            'Repository content size is invalid.'
        ),
        'Repository configuration is too large.': _(
            'Repository configuration is too large.'
        ),
        'YAML aliases are not supported in imported configuration.': _(
            'YAML aliases are not supported in imported configuration.'
        ),
        'Repository declares too many domains.': _(
            'Repository declares too many domains.'
        ),
        'Invalid repository content encoding.': _(
            'Invalid repository content encoding.'
        ),
        'Repository configuration is not UTF-8.': _(
            'Repository configuration is not UTF-8.'
        ),
        'Repository inspection exceeded its safety budget.': _(
            'Repository inspection exceeded its safety budget.'
        ),
        'Imported YAML configuration is too complex.': _(
            'Imported YAML configuration is too complex.'
        ),
        'Imported YAML configuration is too deeply nested.': _(
            'Imported YAML configuration is too deeply nested.'
        ),
        'Repository branch has no valid commit.': _(
            'Repository branch has no valid commit.'
        ),
        'Repository tree is too large.': _('Repository tree is too large.'),
        'Repository manifest is too large.': _(
            'Repository manifest is too large.'
        ),
        'Repository manifest is too complex.': _(
            'Repository manifest is too complex.'
        ),
    }
    return messages.get(
        str(error), _('Could not inspect the repository safely.'),
    )


def _translate_inspection_warning(message):
    """Translate stable non-fatal inspector warnings at request time."""
    if message == (
        'A submodule branch could not be verified within the safety '
        'budget and was normalized to the project version. Review it '
        'before deploying.'
    ):
        return _(
            'A submodule branch could not be verified within the safety '
            'budget and was normalized to the project version. Review it '
            'before deploying.'
        )
    return message


def _github_import_clients(env):
    """Build App, PAT and anonymous clients without materializing a token."""
    service = env['cloud.github.credential.service'].sudo()
    clients = []
    with suppress(Exception):
        clients.append(GitHubAppClient(service.get_credentials()))
    with suppress(Exception):
        pat = service.get_pat()
        if pat:
            clients.append(GitHubPATClient(pat))
    clients.append(GitHubAnonymousClient())
    return clients


def _to_https(url):
    """Normalise a git remote (``git@host:path`` / https) to an HTTPS URL."""
    try:
        return _api_to_https(url)
    except ImportInspectionError:
        return (url or '').strip()


def _version_from_manifest_text(text):
    """Return the Odoo series ('18.0') from an ``__manifest__.py`` source.

    The manifest ``version`` field (e.g. ``"18.0.1.0.0"``) starts with the
    Odoo series. Parsed with the inspector's safe literal parser; returns ''
    when the file is unparseable, its version is not in a series' format (at
    least four parts) or the series is one the panel has no card for. A
    series the panel does not offer is returned: the import refuses it.
    """
    return _api_version_from_manifest_text(text)


def _not_a_repo_url(url):
    """Say why *url* is not a repository address the panel reads, or nothing.

    The repository form refuses such an address when it is saved
    (``cloud.repo.mixin``), but the routes that read the repository
    while the person types parsed it inside each credential attempt. A
    branch folder pasted from the browser (``…/tree/staging/addons``)
    was taken for a missing GitHub App, answered "No GitHub credentials
    configured", and its parse error, logged as an ERROR, raised a log
    alert on the customer's panel (12-oct-2026). A repository of a
    configured Git provider (GitLab) is one; another host is named, as
    the form names it.

    :param str url: what the person typed.
    :returns: None, or the form's own message, translated.
    """
    with suppress(ValueError):
        _parse_github_repo_path(url)
        return None
    if resolve_repo_ref(request.env, url):
        return None
    host = _url_host(url)
    if host and host not in ('github.com', 'www.github.com'):
        return _(
            "There is no Git provider for %s: add it under "
            "Settings → Git providers.",
        ) % host
    return _(
        "Repository URL '%s' is not a recognised GitHub "
        "URL. Use the canonical form "
        "'https://github.com/<owner>/<repo>'.",
    ) % (url or '')


def _url_refusal(url, **shape):
    """Return the answer of a read route to a URL it cannot read, or None.

    :param str url: what the person typed
    :param shape: the fields the SPA reads even on a failure
    :return: ``{ok: False, error, …}``, with ``settings_hint`` pointing
        to the Git providers when the host has none
    :rtype: dict or None
    """
    problem = _not_a_repo_url(url)
    if not problem:
        return None
    answer = {'ok': False, 'error': problem} | shape
    host = _url_host(url)
    if host and host not in ('github.com', 'www.github.com'):
        answer['settings_hint'] = 'git_providers'
    return answer


class GitHubMixin:
    """GitHub App endpoints, repo introspection, project import."""

    # ── GitHub App endpoints ─────────────────────────────────────────────────

    @http.route(['/cloud/get_github_app'], type='json', auth='user')
    def cloud_get_github_app(self):
        # Manager-gated like save_github_app. The read uses sudo, so
        # without this any logged-in user (even without a cloud role)
        # could see the app_id/installation_id/slug.
        self._sec()._check_can_manage_settings()
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
            'has_private_key': _has_encrypted(app.sudo(), 'private_key'),
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
        """Return branch names for a GitHub repo, or a Git provider's.

        A Git provider's repository (GitLab) is read with ``ls-remote``
        (:meth:`_gitlab_read`); the rest of this text is about GitHub.

        Tries the App, then the PAT, then GitHub's anonymous API: a public
        repository needs no credentials, which is what the import itself
        assumes (``_github_import_clients``). Without the last step the
        import dialog told a customer importing a public repository that
        no credentials were configured "to access private repositories",
        and left the branch list empty (8-oct-2026).
        """
        self._sec()._check_cloud_group('group_cloud_consultant')
        refusal = _url_refusal(url, branches=[])
        if refusal:
            return refusal
        if self._is_gitlab(url):
            heads, error = self._gitlab_read(
                url, lambda s: s.reader.ls_remote(
                    s.ref, s.credential, ['refs/heads/*'],
                ),
            )
            if error:
                return {**error, 'branches': []}
            return {
                'ok': True,
                'branches': sorted(
                    name.removeprefix('refs/heads/') for name in heads
                ),
                'provider': 'gitlab',
                'host': resolve_repo_ref(request.env, url).authority,
            }

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

        branches, error = self._github_read(_fetch, 'branches', url)
        if error:
            return {**error, 'branches': []}
        return {'ok': True, 'branches': branches}

    def _github_read(self, fetch, tag, url):
        """Run *fetch* with the App, then the PAT, then anonymously.

        One ladder for every route that reads a repository, so each
        answers the same way: a public repository needs no credentials,
        and when nothing answers the error says what to configure
        (``settings_hint`` lets the editor link to the settings). A
        repository the user may not read with the panel's credentials
        (:func:`may_read_with_panel_credentials`) is read anonymously
        only.

        :param fetch: called with a GitHub client; returns the value.
        :param str tag: names the route in the log.
        :param str url: the repository the user asked for.
        :return: ``(value, None)``, or ``(None, error)`` with *error* the
            response to give: ``ok``, ``error`` and ``settings_hint``.
        """
        if not may_read_with_panel_credentials(request.env, url):
            return self._github_read_public(fetch, tag)
        svc = request.env['cloud.github.credential.service'].sudo()
        app_status = 'not_configured'  # not_configured | no_access | error
        pat_status = 'not_configured'

        # 1. Try GitHub App first (has org-level access)
        try:
            creds = svc.get_credentials()
            return fetch(GitHubAppClient(creds)), None
        except GitHubAPIError as exc:
            _logger.info("[%s] App %s: %s", tag, exc.status_code, exc)
            app_status = 'no_access'
            if exc.status_code not in (404, 401, 403):
                app_status = 'error'
        except (ValueError, UserError):
            _logger.debug("[%s] App not configured", tag, exc_info=True)
            # app_status stays not_configured
        except Exception:
            _logger.exception("[%s] App unexpected error", tag)
            app_status = 'error'

        # 2. Fall back to PAT
        pat = svc.get_pat()
        if pat:
            try:
                return fetch(GitHubPATClient(pat)), None
            except GitHubAPIError as exc:
                _logger.info("[%s] PAT %s: %s", tag, exc.status_code, exc)
                pat_status = 'no_access'
            except Exception:
                _logger.exception("[%s] PAT unexpected error", tag)
                pat_status = 'error'

        # 3. A public repository, anonymously. A private one answers 404
        #    here too, and the message below says what to configure.
        value, error = self._github_read_public(fetch, tag)
        if not error:
            return value, None

        msg = self._build_github_error(app_status, pat_status, bool(pat))
        return None, {'ok': False, 'error': msg, 'settings_hint': 'github'}

    @staticmethod
    def _github_read_public(fetch, tag):
        """Run *fetch* anonymously, as for a public repository.

        :param fetch: called with a GitHub client; returns the value.
        :param str tag: names the route in the log.
        :return: ``(value, None)``, or ``(None, error)`` with *error* a
            response that says the repository was not found.
        :rtype: tuple
        """
        try:
            return fetch(GitHubAnonymousClient()), None
        except GitHubAPIError as exc:
            _logger.info("[%s] anonymous %s: %s", tag, exc.status_code, exc)
        except Exception:
            _logger.exception("[%s] anonymous unexpected error", tag)
        return None, {'ok': False, 'error': git_error_message('not_found')}

    @staticmethod
    def _is_gitlab(url):
        """Return whether *url* names a repository of a Git provider.

        :param str url: the repository URL
        :rtype: bool
        """
        ref = resolve_repo_ref(request.env, url)
        return bool(ref and ref.kind == 'gitlab')

    def _gitlab_read(self, url, read):
        """Read a Git provider's repository with the panel's git.

        Counted against ``rate_limit_git_reads_per_hour`` per user: a git
        process weighs more than a call to GitHub's API.

        :param str url: the repository URL
        :param read: called with its ``GitSource``; returns the value
        :return: ``(value, None)``, or ``(None, error)`` with *error* the
            answer to give: ``ok``, ``error`` and ``settings_hint``
        :rtype: tuple
        """
        env = request.env
        if not consume_git_read(env):
            return None, {'ok': False, 'error': _(
                "Git read limit reached. Try again later."
            )}
        try:
            source = resolve_git_source(env, url, for_user=True)
            return read(source), None
        except GitReaderError as exc:
            _logger.info("[git read] %s: %s", url, exc.code)
            return None, {
                'ok': False, 'error': git_error_message(exc.code),
                'settings_hint': 'git_providers',
            }
        except ValueError:
            return None, {'ok': False, 'error': git_error_message('not_found')}

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
        """Return the HEAD commit SHA of a branch (for freeze).

        The same ladder as the branch list, so a panel with no GitHub
        credentials pins a public repository, and one that cannot read
        the repository is told what to configure rather than "Unexpected
        error" (a customer's panel, 2026-10-15). A branch that is not in
        a repository GitHub does show is named as the problem.
        """
        self._sec()._check_cloud_group('group_cloud_consultant')
        refusal = _url_refusal(url)
        if refusal:
            return refusal
        if self._is_gitlab(url):
            if not is_safe_git_ref(branch or ''):
                return {'ok': False, 'error': git_error_message('not_found')}
            heads, error = self._gitlab_read(
                url, lambda s: s.reader.ls_remote(
                    s.ref, s.credential, [f'refs/heads/{branch}'],
                ),
            )
            if error:
                return error
            sha = heads.get(f'refs/heads/{branch}')
            if not sha:
                return {'ok': False, 'error': _(
                    "Branch %(branch)s does not exist in this repository.",
                    branch=branch,
                )}
            return {'ok': True, 'sha': sha}
        owner, repo = _parse_github_repo_path(url)

        def _fetch(client):
            data = client.get(
                f'/repos/{_gh_seg(owner)}/{_gh_seg(repo)}'
                f'/branches/{_gh_seg(branch)}'
            )
            return data['commit']['sha']

        sha, error = self._github_read(_fetch, 'branch_head', url)
        if not error:
            return {'ok': True, 'sha': sha}
        with suppress(Exception):
            GitHubAnonymousClient().get(f'/repos/{_gh_seg(owner)}/{_gh_seg(repo)}')
            return {
                'ok': False,
                'error': _(
                    "Branch %(branch)s does not exist in this repository.",
                    branch=branch,
                ),
            }
        return error

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
        """Return Odoo addon module names found at the root of a branch.

        Of GitHub through its API, of a Git provider with git.
        """
        self._sec()._check_cloud_group('group_cloud_consultant')
        refusal = _url_refusal(url, modules=[])
        if refusal:
            return refusal
        if self._is_gitlab(url):
            modules, error = self._gitlab_read(
                url, lambda s: git_source_addons(s, branch),
            )
            if error:
                return {**error, 'modules': []}
            return {'ok': True, 'modules': modules}

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

        if not may_read_with_panel_credentials(request.env, url):
            modules, error = self._github_read_public(_fetch, 'repo_modules')
            if error:
                return {**error, 'modules': []}
            return {'ok': True, 'modules': modules}
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
        """Fetch requirements.txt from the root of a repository branch.

        Of GitHub through its API, of a Git provider with git.
        """
        self._sec()._check_cloud_group('group_cloud_consultant')

        refusal = _url_refusal(url, content='', found=False)
        if refusal:
            return refusal
        if self._is_gitlab(url):
            content, error = self._gitlab_read(
                url, lambda s: git_source_requirements(s, branch),
            )
            if error:
                return {**error, 'content': '', 'found': False}
            if content is None:
                return {'ok': True, 'found': False, 'content': ''}
            return {'ok': True, 'found': True, 'content': content}
        owner, repo_name = _parse_github_repo_path(url)

        def _fetch(client):
            data = client.get(
                f'/repos/{_gh_seg(owner)}/{_gh_seg(repo_name)}'
                f'/contents/requirements.txt?ref={_gh_seg(branch)}'
            )
            return base64.b64decode(
                data.get('content', '').replace('\n', '').replace(' ', '')
            ).decode('utf-8')

        if not may_read_with_panel_credentials(request.env, url):
            content, error = self._github_read_public(
                _fetch, 'repo_requirements',
            )
            if error:
                return {'ok': True, 'found': False, 'content': ''}
            return {'ok': True, 'found': True, 'content': content}
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
        """Inspect submodules through the bounded GitHub API workflow.

        The preview of an import, which creates a project: it takes a
        Project Manager, as the import does, checked before anything is
        read.
        """
        self._sec()._check_can_create_project()

        try:
            parse_github_repo_url(repo_url)
            branch = validate_branch(branch)
        except ImportInspectionError as exc:
            return {'ok': False, 'error': _translated_inspection_error(exc)}
        if not try_advisory_xact_lock(
            request.env.cr, _GITHUB_IMPORT_LOCK_NAMESPACE,
        ):
            return {
                'ok': False,
                'error': _(
                    'Another GitHub preview or import is already running. '
                    'Try again when it finishes.'
                ),
            }
        if not consume_hourly_quota(
            request.env,
            f'github_preview_user:{request.env.user.id}',
            'rate_limit_github_previews_per_hour',
        ):
            return {
                'ok': False,
                'error': _(
                    'GitHub preview limit reached. Try again later.'
                ),
            }
        try:
            inspector = GitHubImportInspector(
                _github_import_clients(request.env),
            )
            result = inspector.inspect_preview(repo_url, branch)
            if result.get('sha_warning'):
                result['sha_warning'] = _translate_inspection_warning(
                    result['sha_warning'],
                )
            return result
        except ImportInspectionError as exc:
            return {'ok': False, 'error': _translated_inspection_error(exc)}
        except Exception as exc:
            _logger.warning(
                'Bounded GitHub preview failed (%s)', type(exc).__name__,
            )
            return {
                'ok': False,
                'error': _('Could not inspect the repository safely.'),
            }

    def _create_bounded_github_import(self, inspected, budget_check=None,
                                      budget=None):
        """Create project, repos and production instance from inspected data.

        :param dict inspected: what the import inspector read
        :param budget_check: raises once the import's deadline is spent
        :param budget: the import's ``HTTPBudget``; the addon check after
            the import gets what is left of its time
        """
        def _check_budget():
            """Check the shared import deadline between local operations."""
            if budget_check:
                budget_check()

        _check_budget()
        copier = inspected.get('copier', {})
        repos_data = inspected.get('repos_data', [])
        repo_type = inspected['repo_type']
        odoo_version = inspected.get('odoo_version', '')
        refusal = import_series_refusal(request.env, odoo_version, request.env._(
            'Could not tell which Odoo series this repository is for. Name '
            'its branch after the series (such as 19.0), give its modules '
            'a version that starts with it (such as 19.0.1.0.0) or, for a '
            'Doodba project, set odoo_version in its .copier-answers.yml.'
        ))
        if refusal:
            return {'ok': False, 'error': refusal}
        project_name = (
            copier.get('project_name') or inspected['repo_name']
        )[:255]
        project_values = {
            'name': project_name,
            'odoo_version': odoo_version,
            'pip_dependencies': inspected.get('pip_deps') or None,
            'apt_dependencies': inspected.get('apt_deps') or None,
        }
        if copier.get('project_author'):
            project_values['project_author'] = copier['project_author'][:255]
        if copier.get('project_license'):
            project_values['project_license'] = copier['project_license'][:255]
        project = request.env['cloud.project'].create(project_values)
        _check_budget()

        Repo = request.env['cloud.project.repo'].with_context(
            skip_apply_requirements=True,
        )
        for sequence, repo_data in enumerate(repos_data, start=10):
            _check_budget()
            if not repo_data.get('url'):
                continue
            Repo.create({
                'project_id': project.id,
                'sequence': sequence,
                'url': repo_data['url'],
                'branch': repo_data.get('branch', 'main'),
                'addons': repo_data.get('addons', ''),
                'excludes': repo_data.get('excludes', ''),
                'commit_sha': repo_data.get('commit_sha', ''),
            })
            apply_requirements_content(
                project,
                repo_data.get('requirements', ''),
                repo_data['url'],
                repo_data.get('branch', 'main'),
            )
            _check_budget()

        instance_values = {
            'name': 'production',
            'project_id': project.id,
            'environment': 'production',
            'odoo_version': odoo_version,
        }
        if inspected.get('odoo_conf'):
            instance_values['odoo_conf'] = inspected['odoo_conf']
        if repo_type == 'doodba' and copier:
            copied_fields = (
                'postgres_version', 'postgres_dbname', 'postgres_username',
                'odoo_proxy', 'smtp_relay_host', 'smtp_relay_port',
                'smtp_relay_user',
            )
            for field_name in copied_fields:
                if copier.get(field_name):
                    instance_values[field_name] = copier[field_name]
            domains = copier.get('domains', [])
            if domains:
                instance_values['domain_ids'] = [
                    (0, 0, {
                        'hostname': domain['hostname'],
                        'redirect_to': domain.get('redirect_to', ''),
                    })
                    for domain in domains if domain.get('hostname')
                ]

        autoassign = (
            request.env['ir.config_parameter'].sudo()
            .get_param('incubacloud.host_autoassign', '0') == '1'
        )
        # The dialog has no host to pick: with none to give it, the
        # instance comes without one, and the answer says so rather than
        # leaving the import to look placed.
        host_warning = ''
        if autoassign:
            cpus, ram = request.env['cloud.instance']._compute_instance_resources(
                instance_values,
            )
            best_host = request.env['cloud.host'].select_best_host(cpus, ram)
            if best_host:
                instance_values['host_id'] = best_host.id
            else:
                host_warning = _(
                    'No host could take this instance automatically, so it '
                    'was created without one. Choose its host before '
                    'deploying it.'
                )
        _check_budget()
        instance = request.env['cloud.instance'].create(instance_values)
        _check_budget()
        result = {
            'ok': True,
            'project_id': project.id,
            'instance_id': instance.id,
            'project_name': project.name,
            'repo_type': repo_type,
            'repo_count': len(repos_data),
        }
        if inspected.get('sha_warning'):
            result['sha_warning'] = _translate_inspection_warning(
                inspected['sha_warning'],
            )
        if host_warning:
            result['host_warning'] = host_warning
        # Here, where the repositories came from, rather than at the
        # first deploy: the import is where the customer can still make
        # sense of a module two of them define. Only within the import's
        # time: a spent budget leaves it to the deploy's pre-flight
        # rather than fail an import that is done.
        git_budget = None
        try:
            _check_budget()
        except ImportInspectionError:
            conflicts = []
        else:
            seconds = budget.remaining_seconds() if budget else None
            git_budget = GitReadBudget(request.env, seconds=seconds)
            conflicts = instance._check_addon_conflicts(
                seconds=seconds, git_budget=git_budget,
            )
        warnings = []
        if conflicts:
            names = ', '.join(c['addon'] for c in conflicts[:5])
            if len(conflicts) > 5:
                names += f' (+{len(conflicts) - 5})'
            warnings.append(_(
                '%(count)s module(s) are defined in more than one '
                'repository: %(names)s. Choose which repository keeps each '
                'one in Alerts before deploying.',
                count=len(conflicts), names=names,
            ))
        # GitLab repositories the import's reads had no room for.
        skipped = git_budget and git_budget.addons_warning()
        if skipped:
            warnings.append(skipped)
        if warnings:
            result['addon_conflict_warning'] = ' '.join(warnings)
        return result

    # ── Project import (unified: doodba / odoo.sh / simple) ─────────────────

    @http.route(['/cloud/import_project'], type='json', auth='user')
    def cloud_import_project(self, url, branch='main'):
        """Import a GitHub repo as project + production instance.

        Detects repo type (doodba / odoo.sh / simple), parses config,
        creates project with repos, then creates a production instance
        inheriting project defaults + type-specific config. A repository
        that names no series, or one the user may not choose, is refused
        before anything is created (``import_series_refusal``). It creates
        a project, so it takes a Project Manager, checked before the
        repository is read.
        """

        self._sec()._check_can_create_project()

        try:
            parse_github_repo_url(url)
            branch = validate_branch(branch)
        except ImportInspectionError as exc:
            return {'ok': False, 'error': _translated_inspection_error(exc)}
        if not try_advisory_xact_lock(
            request.env.cr, _GITHUB_IMPORT_LOCK_NAMESPACE,
        ):
            return {
                'ok': False,
                'error': _(
                    'Another GitHub preview or import is already running. '
                    'Try again when it finishes.'
                ),
            }
        if not consume_hourly_quota(
            request.env,
            f'github_import_user:{request.env.user.id}',
            'rate_limit_github_imports_per_hour',
        ):
            return {
                'ok': False,
                'error': _('GitHub import limit reached. Try again later.'),
            }
        try:
            inspector = GitHubImportInspector(
                _github_import_clients(request.env),
            )
            inspected = inspector.inspect_import(url, branch)
        except ImportInspectionError as exc:
            return {'ok': False, 'error': _translated_inspection_error(exc)}
        except Exception as exc:
            _logger.warning(
                'Bounded GitHub import failed (%s)', type(exc).__name__,
            )
            return {
                'ok': False,
                'error': _('Could not inspect the repository safely.'),
            }
        try:
            with request.env.cr.savepoint():
                return self._create_bounded_github_import(
                    inspected, budget_check=inspector.ensure_budget,
                    budget=inspector.budget,
                )
        except ImportInspectionError as exc:
            return {'ok': False, 'error': _translated_inspection_error(exc)}
