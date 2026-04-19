"""Utilities for repo addon/requirement management.

Includes:
- Repo alias and addon list helpers (used by deploy/rebuild executors)
- Pre-flight addon conflict detection across repos
- Pip requirement merging with git-style conflict markers
- GitHub API helpers for fetching module lists and requirements.txt

Conflicts are stored as git-style markers in pip_dependencies so the user
can see exactly which repo introduced each version:

    <<<<<<< existing
    requests==2.28.0
    =======
    requests>=2.30.0
    >>>>>>> org/repo (main)

The field is the single source of truth.  Conflict detection at deploy
time reads the field directly — no alert lookup needed.
"""

import asyncio
import base64
import json
import logging
import re
import urllib.error
import urllib.request
from contextlib import suppress

from odoo.exceptions import UserError

from ..github.client import GitHubAppClient, GitHubAPIError, GitHubPATClient

_logger = logging.getLogger(__name__)


# ── Repo helpers ───────────────────────────────────────────────────────────

def _repo_alias(url):
    """Derive a short alias from a repo URL: last path component without .git."""
    url = (url or "").strip().rstrip("/").removesuffix(".git")
    return url.split("/")[-1] or ""


def _parse_repo_addons(addons_str):
    """Parse addons spec into a list; empty string means all (*)."""
    if not (addons_str or "").strip():
        return ["*"]
    items = [m.strip() for m in re.split(r"[,\n]", addons_str) if m.strip()]
    return items or ["*"]


def _apply_excludes(base_list, excludes_str):
    """Remove excluded names from *base_list*.

    *base_list* must be a resolved list (no ``"*"``).
    Returns the filtered list, or ``["*"]`` if nothing remains.
    """
    excl = {
        m.strip()
        for m in re.split(r"[,\n]", excludes_str or "")
        if m.strip()
    }
    result = [m for m in base_list if m not in excl]
    return result or ["*"]


# ── Conflict marker pattern ────────────────────────────────────────────────

CONFLICT_RE = re.compile(
    r'<<<<<<< (?P<existing_src>[^\n]*)\n'
    r'(?P<existing_line>[^\n]*)\n'
    r'=======\n'
    r'(?P<incoming_line>[^\n]*)\n'
    r'>>>>>>> (?P<incoming_src>[^\n]*)',
)


# ── GitHub URL parser ──────────────────────────────────────────────────────

def _parse_github_repo_path(url):
    url = (url or '').strip().rstrip('/').removesuffix('.git')
    m = re.search(r'github\.com[:/]([^/]+)/([^/]+)$', url)
    if not m:
        raise ValueError(f"Cannot parse GitHub URL: {url!r}")
    return m.group(1), m.group(2)


# ── Pip requirement line parser ────────────────────────────────────────────

def _parse_req_line(line):
    """Return (lowercase_name, raw_spec) or None."""
    stripped = line.split('#')[0].strip()
    if not stripped:
        return None
    m = re.match(r'^([A-Za-z0-9_.-]+)', stripped)
    return (m.group(1).lower(), stripped) if m else None


# ── Source label ───────────────────────────────────────────────────────────

def _format_source(repo_url, repo_branch):
    """Return a short human-readable label for conflict markers."""
    if not repo_url:
        return 'unknown'
    m = re.search(r'github\.com[:/](.+)', (repo_url or '').rstrip('/'))
    name = m.group(1).removesuffix('.git') if m else repo_url.rstrip('/')
    return f'{name} ({repo_branch})' if repo_branch else name


# ── Conflict detection (field-based) ──────────────────────────────────────

def has_pip_conflicts(text):
    """True if pip_dependencies contains unresolved conflict markers."""
    return bool(text and '<<<<<<< ' in text)


def detect_pip_conflicts(text):
    """Parse conflict markers in pip_dependencies text.

    Returns a list of dicts:
        {name, existing, existing_source, incoming, incoming_source}
    """
    conflicts = []
    for m in CONFLICT_RE.finditer(text or ''):
        existing_line = m.group('existing_line').strip()
        parsed = _parse_req_line(existing_line)
        if parsed:
            conflicts.append({
                'name': parsed[0],
                'existing': existing_line,
                'existing_source': m.group('existing_src').strip(),
                'incoming': m.group('incoming_line').strip(),
                'incoming_source': m.group('incoming_src').strip(),
            })
    return conflicts


# ── Merge helpers ──────────────────────────────────────────────────────────

def _replace_whole_line(text, old_line, new_block):
    """Replace *old_line* as a complete line with *new_block*."""
    return re.sub(
        r'(?m)^' + re.escape(old_line) + r'$',
        new_block, text, count=1,
    )


def _build_pkg_map(text):
    """Parse *text* into ``{name: entry}``.

    Entry keys: ``type`` ('clean'|'conflict'), ``spec``, ``block``,
    ``existing_src`` (conflict only).
    """
    pkg_map = {}
    for m in CONFLICT_RE.finditer(text or ''):
        existing_line = m.group('existing_line').strip()
        parsed = _parse_req_line(existing_line)
        if parsed:
            pkg_map[parsed[0]] = {
                'type': 'conflict',
                'spec': parsed[1],
                'existing_src': m.group('existing_src').strip(),
                'block': m.group(0),
            }
    clean_only = CONFLICT_RE.sub('', text or '')
    for line in clean_only.split('\n'):
        parsed = _parse_req_line(line)
        if parsed and parsed[0] not in pkg_map:
            pkg_map[parsed[0]] = {
                'type': 'clean', 'spec': parsed[1], 'block': None,
            }
    return pkg_map


# ── Merge logic ────────────────────────────────────────────────────────────

def merge_pip_requirements(
    existing_text, incoming_text, repo_url=None, repo_branch=None,
):
    """Merge *incoming_text* requirements into *existing_text*.

    - New packages are appended.
    - Exact duplicates are silently skipped.
    - Conflicts (same package, different spec) become git-style markers
      that show the repo/branch origin of each version.  If a conflict
      marker already exists for the package, only the incoming side and
      its source label are updated.

    Returns ``{'content': str, 'conflicts': list}`` where each conflict
    has ``{name, existing, existing_source, incoming, incoming_source}``.
    """
    source_label = _format_source(repo_url, repo_branch)
    pkg_map = _build_pkg_map(existing_text)
    new_text = existing_text or ''
    added = []
    conflicts = []

    for line in (incoming_text or '').split('\n'):
        parsed = _parse_req_line(line)
        if not parsed:
            continue
        name, raw = parsed

        if name not in pkg_map:
            added.append(raw)
            pkg_map[name] = {'type': 'clean', 'spec': raw, 'block': None}

        elif pkg_map[name]['spec'] == raw:
            pass  # exact match — skip

        elif pkg_map[name]['type'] == 'conflict':
            # Update the incoming side of the existing conflict block
            old_block = pkg_map[name]['block']
            existing_src = pkg_map[name]['existing_src']
            existing_spec = pkg_map[name]['spec']
            new_block = (
                f'<<<<<<< {existing_src}\n'
                f'{existing_spec}\n'
                f'=======\n'
                f'{raw}\n'
                f'>>>>>>> {source_label}'
            )
            new_text = new_text.replace(old_block, new_block, 1)
            pkg_map[name]['block'] = new_block
            conflicts.append({
                'name': name,
                'existing': existing_spec,
                'existing_source': existing_src,
                'incoming': raw,
                'incoming_source': source_label,
            })

        else:
            # Clean line → introduce a new conflict marker
            existing_spec = pkg_map[name]['spec']
            new_block = (
                f'<<<<<<< existing\n'
                f'{existing_spec}\n'
                f'=======\n'
                f'{raw}\n'
                f'>>>>>>> {source_label}'
            )
            new_text = _replace_whole_line(
                new_text, existing_spec, new_block,
            )
            pkg_map[name] = {
                'type': 'conflict',
                'spec': existing_spec,
                'existing_src': 'existing',
                'block': new_block,
            }
            conflicts.append({
                'name': name,
                'existing': existing_spec,
                'existing_source': 'existing',
                'incoming': raw,
                'incoming_source': source_label,
            })

    if added:
        base = new_text.rstrip('\n')
        sep = '\n' if base else ''
        new_text = base + sep + '\n'.join(added)

    return {'content': new_text, 'conflicts': conflicts}


# ── Alert helper ───────────────────────────────────────────────────────────

def create_pip_conflict_alert(
    env, conflicts, instance_id=None, project_id=None,
):
    """Create or update a pip_conflict alert for the given target."""
    if not conflicts or (not instance_id and not project_id):
        return None
    Alert = env['cloud.alert']
    domain = [('code', '=', 'pip_conflict'), ('state', '=', 'active')]
    if instance_id:
        domain.append(('instance_id', '=', instance_id))
    else:
        domain.append(('project_id', '=', project_id))
    names = ', '.join(c['name'] for c in conflicts[:3])
    if len(conflicts) > 3:
        names += f' (+{len(conflicts) - 3} more)'
    message = f"Pip dependency conflicts: {names}"
    existing = Alert.search(domain, limit=1)
    if existing:
        existing.write({'conflict_data': conflicts, 'message': message})
        return existing
    vals = {
        'code': 'pip_conflict',
        'level': 'warning',
        'message': message,
        'conflict_data': conflicts,
    }
    if instance_id:
        vals['instance_id'] = instance_id
    else:
        vals['project_id'] = project_id
    return Alert.create(vals)


# ── GitHub fetch ───────────────────────────────────────────────────────────

def fetch_repo_addons(env, url, branch):
    """Fetch Odoo addon module names found at the root of a GitHub repo branch.

    Uses the recursive tree API to find dirs with ``__manifest__.py``.
    Tries: PAT → GitHub App → unauthenticated.
    Returns a sorted list of module names, or ``None`` on failure.
    """
    try:
        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError:
            return None

        def _get_addons(client):
            tree = client.get(
                f'/repos/{owner}/{repo_name}'
                f'/git/trees/{branch}?recursive=1'
            )
            # Extract addon name (parent dir of __manifest__.py)
            # at any depth: root/mod/__manifest__.py or mod/__manifest__.py
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in tree.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })

        svc = env['cloud.github.credential.service'].sudo()

        with suppress(GitHubAPIError, ValueError, UserError, Exception):
            pat = svc.get_pat()
            if pat:
                return _get_addons(GitHubPATClient(pat))

        with suppress(GitHubAPIError, ValueError, UserError, Exception):
            return _get_addons(GitHubAppClient(svc.get_credentials()))

        with suppress(Exception):
            api_url = (
                f'https://api.github.com/repos/{owner}/{repo_name}'
                f'/git/trees/{branch}?recursive=1'
            )
            req = urllib.request.Request(api_url, headers={
                'User-Agent': 'incubacloud/1.0',
                'Accept': 'application/vnd.github+json',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in data.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })

        return None

    except Exception:
        _logger.debug(
            "Unexpected error fetching addons for %s@%s",
            url, branch, exc_info=True,
        )
        return None


def fetch_requirements_txt(env, url, branch):
    """Fetch *requirements.txt* from the root of a GitHub repo branch.

    Tries: PAT → GitHub App → unauthenticated.
    Returns the file content as str, or ``None`` on failure.
    """
    try:
        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError:
            return None

        def _decode(client):
            data = client.get(
                f'/repos/{owner}/{repo_name}'
                f'/contents/requirements.txt?ref={branch}'
            )
            return base64.b64decode(
                data.get('content', '').replace('\n', '').replace(' ', '')
            ).decode('utf-8')

        svc = env['cloud.github.credential.service'].sudo()

        # 1. PAT
        try:
            pat = svc.get_pat()
            if pat:
                return _decode(GitHubPATClient(pat))
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return None
        except (ValueError, UserError):
            _logger.debug("fetch_requirements_txt PAT not configured", exc_info=True)
        except Exception:
            _logger.debug("fetch_requirements_txt PAT unexpected error", exc_info=True)

        # 2. GitHub App
        try:
            return _decode(GitHubAppClient(svc.get_credentials()))
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return None
        except (ValueError, UserError):
            _logger.debug("fetch_requirements_txt App not configured", exc_info=True)
        except Exception:
            _logger.debug("fetch_requirements_txt App unexpected error", exc_info=True)

        # 3. Unauthenticated fallback (public repos)
        try:
            api_url = (
                f'https://api.github.com/repos/{owner}/{repo_name}'
                f'/contents/requirements.txt?ref={branch}'
            )
            req = urllib.request.Request(api_url, headers={
                'User-Agent': 'incubacloud/1.0',
                'Accept': 'application/vnd.github+json',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                content = data.get('content', '')
                raw = content.replace('\n', '').replace(' ', '')
                return base64.b64decode(raw).decode('utf-8')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
        except Exception:
            _logger.debug("fetch_requirements_txt unauthenticated fallback error", exc_info=True)

        return None

    except Exception:
        _logger.debug(
            "Unexpected error fetching requirements.txt for %s@%s",
            url, branch, exc_info=True,
        )
        return None


# ── Pre-flight addon conflict detection ───────────────────────────────────

# ── Instance ↔ Project comparison ──────────────────────────────────────

def _normalize_url(url):
    """Normalize a repo URL for comparison (strip, lowercase, remove .git)."""
    return (url or '').strip().rstrip('/').removesuffix('.git').lower()


def _parse_dep_lines(text):
    """Parse dependency text into {normalized_key: raw_line} map.

    For pip: key = lowercase package name (via _parse_req_line).
    For apt: key = stripped lowercase line.
    Skips empty lines, comments, and conflict marker blocks.
    """
    # Strip conflict markers first
    clean = CONFLICT_RE.sub('', text or '')
    result = {}
    for line in clean.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        parsed = _parse_req_line(stripped)
        if parsed:
            result[parsed[0]] = stripped
        else:
            # Non-pip line (apt, or git+ URL without version)
            result[stripped.lower()] = stripped
    return result


def compare_instance_project(instance):
    """Compare instance deps/repos with its project template.

    Returns a dict with ``pip``, ``apt``, ``repos`` sections.
    Each section has: ``only_instance``, ``only_project``,
    ``conflicts``, ``common`` lists.

    Raises ValueError if either side has unresolved pip conflict markers.
    """
    project = instance.project_id
    if not project:
        raise ValueError("Instance has no project")

    if has_pip_conflicts(instance.pip_dependencies):
        raise ValueError(
            "Instance has unresolved pip conflicts. "
            "Resolve them before syncing."
        )
    if has_pip_conflicts(project.pip_dependencies):
        raise ValueError(
            "Project has unresolved pip conflicts. "
            "Resolve them before syncing."
        )

    pip_diff = _compare_deps(
        instance.pip_dependencies or '',
        project.pip_dependencies or '',
    )
    apt_diff = _compare_deps(
        instance.apt_dependencies or '',
        project.apt_dependencies or '',
    )
    repos_diff = _compare_repos(instance.repo_ids, project.repo_ids)

    return {
        'pip': pip_diff,
        'apt': apt_diff,
        'repos': repos_diff,
    }


def _compare_deps(instance_text, project_text):
    """Compare two dependency texts line by line."""
    inst_map = _parse_dep_lines(instance_text)
    proj_map = _parse_dep_lines(project_text)

    only_instance, only_project, conflicts, common = [], [], [], []

    for key in sorted(set(inst_map) | set(proj_map)):
        in_inst = key in inst_map
        in_proj = key in proj_map
        if in_inst and in_proj:
            if inst_map[key] == proj_map[key]:
                common.append(inst_map[key])
            else:
                conflicts.append({
                    'name': key,
                    'instance_line': inst_map[key],
                    'project_line': proj_map[key],
                })
        elif in_inst:
            only_instance.append(inst_map[key])
        else:
            only_project.append(proj_map[key])

    return {
        'only_instance': only_instance,
        'only_project': only_project,
        'conflicts': conflicts,
        'common': common,
    }


def _compare_repos(inst_repos, proj_repos):
    """Compare instance repos with project repos by normalized URL."""
    inst_map = {_normalize_url(r.url): r for r in inst_repos}
    proj_map = {_normalize_url(r.url): r for r in proj_repos}

    only_instance, only_project, conflicts, common = [], [], [], []

    for key in sorted(set(inst_map) | set(proj_map)):
        in_inst = key in inst_map
        in_proj = key in proj_map
        if in_inst and in_proj:
            ir, pr = inst_map[key], proj_map[key]
            diffs = {}
            for field in ('branch', 'addons', 'excludes', 'commit_sha'):
                iv = (getattr(ir, field) or '').strip()
                pv = (getattr(pr, field) or '').strip()
                if iv != pv:
                    diffs[field] = {'instance': iv, 'project': pv}
            if diffs:
                conflicts.append({
                    'url': ir.url,
                    'fields': diffs,
                })
            else:
                common.append(ir.url)
        elif in_inst:
            ir = inst_map[key]
            only_instance.append({
                'url': ir.url,
                'branch': ir.branch or 'main',
                'addons': ir.addons or '',
                'excludes': ir.excludes or '',
                'commit_sha': ir.commit_sha or '',
            })
        else:
            pr = proj_map[key]
            only_project.append({
                'url': pr.url,
                'branch': pr.branch or 'main',
                'addons': pr.addons or '',
                'excludes': pr.excludes or '',
                'commit_sha': pr.commit_sha or '',
            })

    return {
        'only_instance': only_instance,
        'only_project': only_project,
        'conflicts': conflicts,
        'common': common,
    }


def apply_sync(instance, actions):
    """Apply user-selected sync actions between instance and project.

    ``actions`` is a dict with keys: ``pip``, ``apt``, ``repos``.
    Each has ``push`` (list of lines/urls to copy instance→project),
    ``pull`` (list to copy project→instance), and ``conflicts``
    (dict of {name: 'instance'|'project'} for conflict resolution).
    """
    project = instance.project_id

    # ── Dependencies ──────────────────────────────────────────────────
    for dep_field in ('pip', 'apt'):
        field_name = f'{dep_field}_dependencies'
        dep_actions = actions.get(dep_field, {})

        push_lines = dep_actions.get('push', [])
        pull_lines = dep_actions.get('pull', [])
        conflict_choices = dep_actions.get('conflicts', {})

        if push_lines:
            current = (getattr(project, field_name) or '').rstrip('\n')
            sep = '\n' if current else ''
            setattr(project, field_name, current + sep + '\n'.join(push_lines))

        if pull_lines:
            current = (getattr(instance, field_name) or '').rstrip('\n')
            sep = '\n' if current else ''
            setattr(instance, field_name, current + sep + '\n'.join(pull_lines))

        if conflict_choices:
            # For each conflict, the user chose which side wins.
            # Replace the line on the losing side with the winning line.
            inst_map = _parse_dep_lines(getattr(instance, field_name) or '')
            proj_map = _parse_dep_lines(getattr(project, field_name) or '')
            for name, winner in conflict_choices.items():
                if winner == 'instance' and name in inst_map and name in proj_map:
                    # Push instance line to project
                    proj_text = getattr(project, field_name) or ''
                    proj_text = _replace_whole_line(
                        proj_text, proj_map[name], inst_map[name],
                    )
                    setattr(project, field_name, proj_text)
                elif winner == 'project' and name in proj_map and name in inst_map:
                    # Pull project line to instance
                    inst_text = getattr(instance, field_name) or ''
                    inst_text = _replace_whole_line(
                        inst_text, inst_map[name], proj_map[name],
                    )
                    setattr(instance, field_name, inst_text)

    # ── Repos ─────────────────────────────────────────────────────────
    repo_actions = actions.get('repos', {})
    push_repos = repo_actions.get('push', [])
    pull_repos = repo_actions.get('pull', [])
    repo_conflicts = repo_actions.get('conflicts', {})

    ProjectRepo = instance.env['cloud.project.repo'].with_context(
        skip_apply_requirements=True,
    )
    InstanceRepo = instance.env['cloud.instance.repo'].with_context(
        skip_apply_requirements=True,
    )

    # Push repos from instance to project
    for url in push_repos:
        norm = _normalize_url(url)
        src = next(
            (r for r in instance.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        if src:
            ProjectRepo.create({
                'project_id': project.id,
                'url': src.url,
                'branch': src.branch or 'main',
                'addons': src.addons or '',
                'excludes': src.excludes or '',
                'commit_sha': src.commit_sha or '',
            })

    # Pull repos from project to instance
    for url in pull_repos:
        norm = _normalize_url(url)
        src = next(
            (r for r in project.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        if src:
            InstanceRepo.create({
                'instance_id': instance.id,
                'url': src.url,
                'branch': src.branch or 'main',
                'addons': src.addons or '',
                'excludes': src.excludes or '',
                'commit_sha': src.commit_sha or '',
            })

    # Resolve repo conflicts (update losing side with winning values)
    for url, winner in repo_conflicts.items():
        norm = _normalize_url(url)
        inst_repo = next(
            (r for r in instance.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        proj_repo = next(
            (r for r in project.repo_ids if _normalize_url(r.url) == norm),
            None,
        )
        if not inst_repo or not proj_repo:
            continue
        if winner == 'instance':
            proj_repo.with_context(skip_apply_requirements=True).write({
                'branch': inst_repo.branch,
                'addons': inst_repo.addons or '',
                'excludes': inst_repo.excludes or '',
                'commit_sha': inst_repo.commit_sha or '',
            })
        else:
            inst_repo.with_context(skip_apply_requirements=True).write({
                'branch': proj_repo.branch,
                'addons': proj_repo.addons or '',
                'excludes': proj_repo.excludes or '',
                'commit_sha': proj_repo.commit_sha or '',
            })


async def detect_addon_conflicts(env, repos):
    """Fetch module lists from all repos and find cross-repo duplicates.

    For each repo, resolves its effective module set by applying the
    ``addons`` and ``excludes`` fields.  When the repo uses a wildcard
    (empty ``addons``), the full module list is fetched from GitHub.

    Returns a list of ``{'addon': str, 'repos': [alias, ...]}``.
    """
    repo_modules: dict[str, set[str]] = {}

    # Resolve credentials in the main thread (env is not thread-safe)
    _client = None
    svc = env['cloud.github.credential.service'].sudo()
    with suppress(Exception):
        pat = svc.get_pat()
        if pat:
            _client = GitHubPATClient(pat)
    if _client is None:
        with suppress(Exception):
            _client = GitHubAppClient(svc.get_credentials())

    def _fetch_addons_with_client(url, branch):
        """Thread-safe: uses pre-resolved client, no env access."""
        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError:
            return None
        def _get(client):
            tree = client.get(
                f'/repos/{owner}/{repo_name}'
                f'/git/trees/{branch}?recursive=1'
            )
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in tree.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })
        if _client:
            with suppress(Exception):
                return _get(_client)
        # Public fallback
        api_url = (
            f'https://api.github.com/repos/{owner}/{repo_name}'
            f'/git/trees/{branch}?recursive=1'
        )
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'incubacloud/1.0',
            'Accept': 'application/vnd.github+json',
        })
        with suppress(Exception):
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return sorted({
                item['path'].rsplit('/', 2)[-2]
                for item in data.get('tree', [])
                if item['path'].endswith('/__manifest__.py')
                and '/' in item['path']
            })
        return None

    async def _fetch_one(repo):
        alias = _repo_alias(repo.url)
        if not alias:
            return
        addons_str = (repo.addons or "").strip()
        excludes_str = (repo.excludes or "").strip()

        if addons_str:
            modules = _parse_repo_addons(addons_str)
            if excludes_str:
                modules = _apply_excludes(modules, excludes_str)
            if modules != ["*"]:
                repo_modules[alias] = set(modules)
                return

        # Wildcard: fetch full list from GitHub
        all_mods = await asyncio.to_thread(
            _fetch_addons_with_client,
            repo.url, repo.branch or "main",
        )
        if all_mods is None:
            _logger.warning(
                "Addon check: could not fetch addons for %s@%s",
                alias, repo.branch,
            )
            return

        _logger.info(
            "Addon check: %s@%s → %d addons",
            alias, repo.branch, len(all_mods),
        )
        if excludes_str:
            filtered = _apply_excludes(all_mods, excludes_str)
            repo_modules[alias] = (
                set(filtered) if filtered != ["*"]
                else set(all_mods)
            )
        else:
            repo_modules[alias] = set(all_mods)

    await asyncio.gather(*(_fetch_one(r) for r in repos))
    _logger.info(
        "Addon check: %d repos, modules: %s",
        len(repo_modules),
        {k: len(v) for k, v in repo_modules.items()},
    )

    # Cross-reference: addons present in 2+ repos
    addon_to_repos: dict[str, list[str]] = {}
    for alias, modules in repo_modules.items():
        for mod in modules:
            addon_to_repos.setdefault(mod, []).append(alias)

    return [
        {"addon": addon, "repos": sorted(repos_list)}
        for addon, repos_list in sorted(addon_to_repos.items())
        if len(repos_list) > 1
    ]
