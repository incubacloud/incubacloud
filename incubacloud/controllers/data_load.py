# Part of Odoo. See LICENSE file for full copyright and licensing details.

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
import yaml
from contextlib import suppress
from datetime import timedelta, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncssh
import boto3
from botocore.exceptions import ClientError, ParamValidationError

from odoo import fields, http, _
from odoo.exceptions import UserError
from odoo.fields import Domain
from odoo.http import Controller, request
from odoo.tools import format_amount, file_open

from ..github.client import GitHubAppClient, GitHubAPIError, GitHubPATClient
from ..models._repo_requirements import (
    CONFLICT_RE,
    _parse_req_line,
    _repo_alias,
    apply_sync,
    compare_instance_project,
    create_pip_conflict_alert,
    detect_addon_conflicts,
    detect_pip_conflicts,
    fetch_requirements_txt,
    merge_pip_requirements,
)
from .async_utils import run_async

# Allowed (model, field) pairs for the /cloud/get_secret endpoint
_SECRET_FIELDS = {
    'cloud.host': {
        'password', 'traefik_panel_password',
    },
    'cloud.instance': {
        'odoo_admin_password', 'postgres_password', 'smtp_relay_password',
    },
    'cloud.backup.backend': {
        's3_secret_access_key', 'passphrase',
    },
}

_logger = logging.getLogger(__name__)


def _normalize_domain(domain):
    """Strip protocol prefix and trailing slashes from a domain string.

    Stores only the bare hostname so that the frontend can always prepend
    the correct scheme (https://) without ending up with a double protocol.
    """
    if not domain:
        return domain
    d = domain.strip()
    for prefix in ('https://', 'http://'):
        d = d.removeprefix(prefix)
    return d.strip('/')


def _parse_github_repo_path(url):
    """Extract (owner, repo) from a GitHub URL.

    Supports https://github.com/owner/repo[.git] and
    git@github.com:owner/repo[.git].
    """
    url = (url or "").strip().rstrip("/").removesuffix(".git")
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+)$", url)
    if not m:
        raise ValueError(f"Cannot parse GitHub URL: {url!r}")
    return m.group(1), m.group(2)


def _has_pat(env):
    """Check if a PAT is configured in cloud.settings."""
    settings = env["cloud.settings"]._get()
    return bool((settings.github_pat or "").strip())


def _job_response(env, job_id):
    """Return a consistent dict for a deploy/rebuild job response.

    If the job is blocked by a pip conflict alert, the response includes
    ``blocked=True`` plus the conflict details so the frontend can display
    a clear notification instead of opening the log page.
    """
    job = env['cloud.job'].browse(job_id)
    alert = job.blocked_alert_id
    if not alert:
        return {'job_id': job_id}
    return {
        'job_id': job_id,
        'blocked': True,
        'alert_id': alert.id,
        'alert_code': alert.code or '',
        'message': alert.message or '',
        'conflicts': alert.conflict_data or [],
    }


class CloudDataLoadController(Controller):

    def _sec(self):
        return request.env['cloud.security.mixin']

    @http.route(['/cloud/get_config'], type='jsonrpc', auth='user')
    def cloud_get_config(self):
        """Return feature flags and config for the SPA.

        Dependent modules should override this method and call super() to inject
        additional feature flags without modifying the core::

            def cloud_get_config(self):
                config = super().cloud_get_config()
                config['features']['vps_provisioning'] = True
                return config
        """
        perms = self._sec()._get_user_permissions()
        user = request.env.user
        return {
            'features': {},
            'role': perms.pop('role', 'stakeholder'),
            'permissions': perms,
            'user': {
                'name': user.name or user.login or '',
                'login': user.login or '',
                'avatar_url': (
                    f'/web/image?model=res.users'
                    f'&id={user.id}&field=image_128'
                ),
            },
        }

    @http.route(['/cloud/get_projects'], type='jsonrpc', auth='user')
    def cloud_get_projects(self):
        projects = request.env['cloud.project'].search([])
        result = []
        for p in projects:
            prod = p.instance_ids.filtered(lambda i: i.environment == 'production')
            first_prod_id = prod[:1].id if prod else False
            result.append({
                'id': p.id,
                'name': p.name,
                'description': p.description,
                'odoo_version': p.odoo_version or '',
                'status': p.status,
                'instance_count': len(p.instance_ids),
                'environments': p.instance_ids.mapped('environment'),
                'first_production_instance_id': first_prod_id,
                'tags': [
                    {'id': t.id, 'name': t.name, 'color': t.color}
                    for t in p.tag_ids
                ],
            })
        return result

    @http.route(['/cloud/get_tags'], type='jsonrpc', auth='user')
    def cloud_get_tags(self):
        tags = request.env['cloud.tag'].search([])
        defaults = request.env['cloud.project'].default_get(
            ['pip_dependencies', 'odoo_version']
        )
        return {
            'tags': [
                {'id': t.id, 'name': t.name, 'color': t.color} for t in tags
            ],
            'project_defaults': {
                'pip_dependencies': defaults.get('pip_dependencies', ''),
                'odoo_version': defaults.get('odoo_version', '19.0'),
            },
        }

    @http.route(['/cloud/get_users'], type='jsonrpc', auth='user')
    def cloud_get_users(self, search='', **kw):
        """Search internal users for member picker."""
        self._sec()._check_cloud_group('group_cloud_project_manager')
        domain = [('share', '=', False)]
        if search:
            domain.append(('name', 'ilike', search))
        users = request.env['res.users'].sudo().search(domain, limit=20)
        return [{'id': u.id, 'name': u.name, 'email': u.email or ''} for u in users]

    @http.route(['/cloud/create_tag'], type='jsonrpc', auth='user')
    def cloud_create_tag(self, name):
        name = name.strip()
        if not name:
            return {'ok': False, 'error': _('Tag name cannot be empty')}
        existing = request.env['cloud.tag'].search([('name', '=ilike', name)], limit=1)
        if existing:
            return {'id': existing.id, 'name': existing.name, 'color': existing.color}
        total = request.env['cloud.tag'].search_count([])
        tag = request.env['cloud.tag'].create({'name': name, 'color': total % 10})
        return {'id': tag.id, 'name': tag.name, 'color': tag.color}

    @http.route(['/cloud/get_hosts'], type='jsonrpc', auth='user')
    def cloud_get_hosts(self):
        hosts = request.env['cloud.host'].search([])
        autoassign = (
            request.env['ir.config_parameter'].sudo()
            .get_param('incubacloud.host_autoassign', '0') == '1'
        )
        return {
            'autoassign_enabled': autoassign,
            'hosts': [
                {
                    'id': host.id,
                    'name': host.name,
                    'type': host.type,
                    'status': host.status,
                    'description': host.description,
                    'tags': host.tags,
                    'ip_address': host.ip_address,
                    'port': host.port,
                    'user': host.user,
                    'login_type': host.login_type,
                    'has_password': bool(host.password),
                    'key_file': host.key_file,
                    'cpu_cores': host.cpu_cores,
                    'ram_total_gb': host.ram_total_gb,
                    'disk': round(host.disk_usage),
                    'disk_free_gb': round(host.disk_free_gb, 1),
                    'last_probed': host.last_probed,
                    'instance_count': len(host.instance_ids),
                    'traefik_deployed': host.traefik_deployed,
                    'exclude_from_autoassign': host.exclude_from_autoassign,
                    'allocated_cpus': round(host.allocated_cpus, 2),
                    'allocated_ram_gb': round(host.allocated_ram_gb, 2),
                    'available_cpus': round(host.available_cpus, 2),
                    'available_ram_gb': round(host.available_ram_gb, 2),
                    'alerts': [
                        {
                            'code': a.code,
                            'message': a.message,
                            'level': a.level,
                        }
                        for a in host.alert_ids.filtered(
                            lambda a: a.state == 'active'
                        )
                    ],
                }
                for host in hosts
            ],
        }

    @http.route(['/cloud/get_dashboard'], type='jsonrpc', auth='user')
    def cloud_get_dashboard(self):
        env = request.env
        hosts = env['cloud.host'].search([])
        instances = env['cloud.instance'].search([])
        projects = env['cloud.project'].search([])
        active_alerts = env['cloud.alert'].search([('state', '=', 'active')])
        CloudJob = env['cloud.job']
        recent_jobs = CloudJob.search(
            [('job_type_id.code', 'not in', CloudJob._hidden_job_types)],
            order='create_date desc', limit=10,
        )
        return {
            'counts': {
                'projects': len(projects),
                'hosts': len(hosts),
                'instances': len(instances),
                'alerts': len(active_alerts),
            },
            'alerts': [
                {
                    'id': a.id,
                    'code': a.code,
                    'host_id': a.host_id.id,
                    'instance_id': a.instance_id.id,
                    'project_id': a.instance_id.project_id.id if a.instance_id else False,
                    'host': (
                        a.host_id.name
                        if a.host_id
                        else f"{a.instance_id.project_id.name} / {a.instance_id.name}"
                    ),
                    'job_id': a.job_id.id,
                    'message': a.message,
                    'level': a.level,
                }
                for a in active_alerts
            ],
            'recent_jobs': [
                {
                    'id': j.id,
                    'name': j.name,
                    'host': j.host_id.name,
                    'state': j.state,
                    'create_date': j.create_date,
                    'job_type': j.job_type_id.code,
                }
                for j in recent_jobs
            ],
        }

    @http.route(['/cloud/get_alert_count'], type='jsonrpc', auth='user')
    def cloud_get_alert_count(self):
        count = request.env['cloud.alert'].search_count([('state', '=', 'active')])
        return {'count': count}

    @http.route(['/cloud/dismiss_alert'], type='jsonrpc', auth='user')
    def cloud_dismiss_alert(self, alert_id):
        request.env['cloud.alert'].browse(alert_id).write(
            {'state': 'dismissed'}
        )
        return True

    @http.route(['/cloud/compare_sync'], type='jsonrpc', auth='user')
    def cloud_compare_sync(self, instance_id):
        """Compare instance deps/repos with its project template."""
        inst = request.env['cloud.instance'].browse(int(instance_id))
        if not inst.exists() or not inst.project_id:
            return {'ok': False, 'error': _('Instance or project not found')}
        try:
            diff = compare_instance_project(inst)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        return {'ok': True, 'diff': diff}

    @http.route(['/cloud/apply_sync'], type='jsonrpc', auth='user')
    def cloud_apply_sync(self, instance_id, actions):
        """Apply user-selected sync actions between instance and project."""
        inst = request.env['cloud.instance'].browse(int(instance_id))
        if not inst.exists() or not inst.project_id:
            return {'ok': False, 'error': _('Instance or project not found')}
        try:
            apply_sync(inst, actions)
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}
        return {'ok': True}

    @http.route(['/cloud/get_alert_history'], type='jsonrpc', auth='user')
    def cloud_get_alert_history(self, state_filter='all'):
        domain = [] if state_filter == 'all' else [('state', '=', state_filter)]
        alerts = request.env['cloud.alert'].search(
            domain, order='create_date desc', limit=500,
        )
        return [
            {
                'id': a.id,
                'code': a.code,
                'level': a.level,
                'state': a.state,
                'message': a.message,
                'create_date': a.create_date,
                'host': (
                    a.host_id.name
                    if a.host_id
                    else (
                        f"{a.instance_id.project_id.name} / {a.instance_id.name}"
                        if a.instance_id
                        else (a.project_id.name or '—')
                    )
                ),
                'target': (
                    a.host_id.name if a.host_id
                    else (
                        f"{a.instance_id.project_id.name} / {a.instance_id.name}"
                        if a.instance_id and a.instance_id.project_id
                        else (a.instance_id.name if a.instance_id
                              else (a.project_id.name or '—'))
                    )
                ),
                'job_id': a.job_id.id,
                'conflict_data': a.conflict_data or [],
                'instance_id': a.instance_id.id or False,
                'project_id': (
                    a.instance_id.project_id.id if a.instance_id
                    else (a.project_id.id or False)
                ),
            }
            for a in alerts
        ]

    @http.route(['/cloud/resolve_pip_conflict'], type='jsonrpc', auth='user')
    def cloud_resolve_pip_conflict(self, alert_id, resolutions):
        """Resolve a pip dependency conflict alert.

        alert_id: int
        resolutions: {package_name_lowercase: chosen_spec_string}
        Returns {'ok': bool, 'error'?: str}
        """
        alert = request.env['cloud.alert'].browse(int(alert_id))
        if (not alert.exists()
                or alert.code != 'pip_conflict'
                or alert.state != 'active'):
            return {
                'ok': False, 'error': _('Alert not found or already resolved'),
            }
        parent = alert.instance_id or alert.project_id
        if not parent:
            return {'ok': False, 'error': _('No parent record found')}

        # Replace each conflict block with the user's chosen spec.
        # Track seen names to deduplicate if the same block appears twice.
        new_text = parent.pip_dependencies or ''
        for m in list(CONFLICT_RE.finditer(parent.pip_dependencies or '')):
            existing_line = m.group('existing_line').strip()
            parsed = _parse_req_line(existing_line)
            if parsed:
                chosen = resolutions.get(parsed[0])
                if chosen is not None:
                    new_text = new_text.replace(m.group(0), chosen, 1)

        parent.pip_dependencies = new_text
        alert.state = 'dismissed'

        # Unblock the linked job only if all conflicts are now resolved
        if not detect_pip_conflicts(new_text):
            blocked_job = request.env['cloud.job'].search(
                [('blocked_alert_id', '=', alert.id)], limit=1,
            )
            if blocked_job:
                blocked_job.unblock_and_enqueue()
        return {'ok': True}

    @http.route(['/cloud/resolve_addon_conflict'], type='jsonrpc', auth='user')
    def cloud_resolve_addon_conflict(self, alert_id, resolutions):
        """Resolve an addon conflict alert.

        alert_id: int
        resolutions: {addon_name: winner_repo_alias}
            For each conflicting addon, the user picks which repo keeps it.
            The addon is added to the excludes of all other repos involved.
        Returns {'ok': bool, 'error'?: str}
        """
        alert = request.env['cloud.alert'].browse(int(alert_id))
        if (not alert.exists()
                or alert.code != 'addon_conflict'
                or alert.state != 'active'):
            return {'ok': False, 'error': _('Alert not found or already resolved')}
        inst = alert.instance_id
        if not inst:
            return {'ok': False, 'error': _('No instance linked to this alert')}

        # For each repo, compute what modules need to be added to its excludes.
        for repo in inst.repo_ids:
            alias = _repo_alias(repo.url)
            existing = {m.strip() for m in (repo.excludes or '').split(',') if m.strip()}
            for conflict in (alert.conflict_data or []):
                addon = conflict['addon']
                winner = resolutions.get(addon)
                # Add to excludes if this repo is a loser and involved in the conflict
                if winner and alias != winner and alias in conflict.get('repos', []):
                    existing.add(addon)
            repo.excludes = ','.join(sorted(existing)) if existing else ''

        alert.state = 'dismissed'
        blocked_job = request.env['cloud.job'].search(
            [('blocked_alert_id', '=', alert.id)], limit=1,
        )
        if blocked_job:
            blocked_job.unblock_and_enqueue()
        return {'ok': True}

    @http.route(['/cloud/get_secret'], type='jsonrpc', auth='user')
    def cloud_get_secret(self, model, record_id, field):
        """Return the plain-text value of an encrypted password field.

        Only fields explicitly listed in _SECRET_FIELDS are accessible.
        The user must have write access to the record (same check as saving).
        """
        self._sec()._check_cloud_group('group_cloud_developer')
        allowed = _SECRET_FIELDS.get(model)
        if not allowed or field not in allowed:
            return {'ok': False, 'error': _('Field not allowed')}
        record = request.env[model].browse(record_id)
        if not record.exists():
            return {'ok': False, 'error': _('Record not found')}
        # Require write access as a proxy for "trusted user"
        record.check_access('write')
        value = getattr(record, field, None) or ''
        return {'value': value}

    @http.route(['/cloud/get_host'], type='jsonrpc', auth='user')
    def cloud_get_host(self, host_id):
        host = request.env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}
        return {
            'id': host.id,
            'name': host.name,
            'type': host.type,
            'status': host.status,
            'ip_address': host.ip_address,
            'port': host.port,
            'user': host.user,
            'login_type': host.login_type,
            'has_password': bool(host.password),
            'description': host.description,
            'tags': host.tags,
            'cpu_cores': host.cpu_cores,
            'ram_total_gb': host.ram_total_gb,
            'disk': round(host.disk_usage),
            'disk_free_gb': round(host.disk_free_gb, 1),
            'last_probed': host.last_probed,
            'instance_count': len(host.instance_ids),
            'traefik_deployed': host.traefik_deployed,
            'wildcard_domain': host.wildcard_domain or '',
            'has_traefik_panel_password': bool(host.traefik_panel_password),
            'traefik_config_yml': host.traefik_config_yml or '',
            'traefik_inverseproxy_yaml': host.traefik_inverseproxy_yaml or '',
            'traefik_yml': host.traefik_yml or '',
            'exclude_from_autoassign': host.exclude_from_autoassign,
            'allocated_cpus': round(host.allocated_cpus, 2),
            'allocated_ram_gb': round(host.allocated_ram_gb, 2),
            'available_cpus': round(host.available_cpus, 2),
            'available_ram_gb': round(host.available_ram_gb, 2),
            'whitelist': host.whitelist_ids.mapped('hostname'),
            'custom_actions': [
                {
                    'code': jt.code,
                    'name': jt.name,
                    'action_icon': jt.action_icon or 'fa-cog',
                }
                for jt in request.env['cloud.job.type'].search([
                    ('show_as_action', '=', True),
                    ('apply_to', '=', 'host'),
                ], order='action_order, id')
            ],
        }

    _HOST_ALLOWED = {
        'name', 'type', 'ip_address', 'port', 'user', 'login_type',
        'password', 'key_file', 'description', 'tags',
        'wildcard_domain', 'traefik_panel_password',
        'traefik_config_yml', 'traefik_inverseproxy_yaml', 'traefik_yml',
        'exclude_from_autoassign',
    }

    @http.route(['/cloud/host_defaults'], type='jsonrpc', auth='user')
    def cloud_host_defaults(self):
        self._sec()._check_can_manage_hosts()
        Host = request.env['cloud.host']
        defaults = Host.default_get([
            'traefik_config_yml', 'traefik_inverseproxy_yaml', 'traefik_yml',
        ])
        return {
            'traefik_config_yml': defaults.get('traefik_config_yml', ''),
            'traefik_inverseproxy_yaml': defaults.get('traefik_inverseproxy_yaml', ''),
            'traefik_yml': defaults.get('traefik_yml', ''),
        }

    @http.route(['/cloud/create_host'], type='jsonrpc', auth='user')
    def cloud_create_host(self, vals):
        self._sec()._check_can_manage_hosts()
        safe = {k: v for k, v in vals.items() if k in self._HOST_ALLOWED}
        if safe.get('port'):
            safe['port'] = int(safe['port'])
        host = request.env['cloud.host'].create(safe)
        return {'id': host.id, 'name': host.name}

    @http.route(['/cloud/delete_host'], type='jsonrpc', auth='user')
    def cloud_delete_host(self, host_id):
        self._sec()._check_can_manage_hosts()
        host = request.env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}
        if host.instance_ids:
            return {'ok': False, 'error': _('Host still has instances. Delete all instances first.')}
        if not host.traefik_deployed:
            # Nothing was deployed remotely — archive directly without SSH
            host.write({'active': False})
            return {'ok': True}
        job_id = request.env['cloud.job'].enqueue(host_id, False, 'delete_host')
        return {'job_id': job_id}

    @http.route(['/cloud/save_host'], type='jsonrpc', auth='user')
    def cloud_save_host(self, host_id, vals):
        self._sec()._check_can_manage_hosts()
        host = request.env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}
        safe_vals = {k: v for k, v in vals.items() if k in self._HOST_ALLOWED}
        # Only update key_file if a new one was uploaded
        if not safe_vals.get('key_file'):
            safe_vals.pop('key_file', None)
        host.write(safe_vals)

        # Sync whitelist entries when provided.
        # Use a direct search instead of host.whitelist_ids to avoid
        # reading a stale ORM cache on a freshly-browsed record.
        if 'whitelist' in vals:
            hostnames = [
                h.strip() for h in (vals['whitelist'] or [])
                if h and h.strip()
            ]
            request.env['cloud.host.whitelist'].search(
                [('host_id', '=', host.id)]
            ).unlink()
            if hostnames:
                request.env['cloud.host.whitelist'].create([
                    {'host_id': host.id, 'hostname': h, 'sequence': i * 10}
                    for i, h in enumerate(hostnames, 1)
                ])

        return {'ok': True}

    @http.route(['/cloud/setup_whitelist'], type='jsonrpc', auth='user')
    def cloud_setup_whitelist(self, host_id):
        self._sec()._check_can_manage_hosts()
        host = request.env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}
        job_id = request.env['cloud.job'].enqueue(
            host_id, False, 'setup_whitelist'
        )
        return {'job_id': job_id}

    # ── Project detail endpoints ────────────────────────────────────────────

    @http.route(['/cloud/get_project'], type='jsonrpc', auth='user')
    def cloud_get_project(self, project_id):
        project = request.env['cloud.project'].browse(project_id)
        if not project.exists():
            return {'ok': False, 'error': _('Project not found')}
        all_tags = request.env['cloud.tag'].search([])
        return {
            'id': project.id,
            'name': project.name,
            'description': project.description or '',
            'project_author': project.project_author or 'IncubaCloud',
            'project_license': project.project_license or 'BSL-1.0',
            'status': project.status,
            'instance_count': len(project.instance_ids),
            'tags': [
                {'id': t.id, 'name': t.name, 'color': t.color}
                for t in project.tag_ids
            ],
            'all_tags': [
                {'id': t.id, 'name': t.name, 'color': t.color}
                for t in all_tags
            ],
            'odoo_version': project.odoo_version or '19.0',
            'odoo_commit_sha': project.odoo_commit_sha or '',
            'odoo_initial_lang': (
                project.odoo_initial_lang.id if project.odoo_initial_lang else None
            ),
            'pip_dependencies': project.pip_dependencies or '',
            'apt_dependencies': project.apt_dependencies or '',
            'member_ids': [{'id': u.id, 'name': u.name} for u in project.member_ids],
            'backup_backend_id': (
                project.backup_backend_id.id if project.backup_backend_id else None
            ),
            'repos': [
                {
                    'id': r.id,
                    'url': r.url,
                    'branch': r.branch or 'main',
                    'addons': r.addons or '',
                    'excludes': r.excludes or '',
                    'commit_sha': r.commit_sha or '',
                }
                for r in project.repo_ids
            ],
        }

    @http.route(['/cloud/create_project'], type='jsonrpc', auth='user')
    def cloud_create_project(self, vals):
        self._sec()._check_can_create_project()
        _PROJ_ALLOWED = {
            'name', 'description', 'project_author', 'project_license',
            'odoo_version', 'odoo_commit_sha', 'odoo_initial_lang',
            'pip_dependencies', 'apt_dependencies',
            'backup_backend_id',
        }
        repos = vals.pop('repos', None)
        tag_ids = vals.pop('tag_ids', None)
        safe = {k: v for k, v in vals.items() if k in _PROJ_ALLOWED}
        # Never override pip_dependencies with an empty string: let the ORM
        # default apply so every new project starts with the base packages.
        if not safe.get('pip_dependencies'):
            safe.pop('pip_dependencies', None)
        if tag_ids is not None:
            safe['tag_ids'] = [fields.Command.set(tag_ids)]
        project = request.env['cloud.project'].create(safe)
        # ── Create repos ─────────────────────────────────────────────────────
        if repos:
            Repo = request.env['cloud.project.repo'].with_context(
                skip_apply_requirements=True,
            )
            for seq, repo in enumerate(repos, start=10):
                if repo.get('url', '').strip():
                    Repo.create({
                        'project_id': project.id,
                        'sequence': seq,
                        'url': repo['url'].strip(),
                        'branch': repo.get('branch', 'main').strip() or 'main',
                        'addons': repo.get('addons', '').strip(),
                        'excludes': repo.get('excludes', '').strip(),
                        'commit_sha': repo.get('commit_sha', '').strip(),
                    })
            # ── Merge repo requirements into pip_dependencies ─────────────
            merged = project.pip_dependencies or ''
            for repo in repos:
                url = repo.get('url', '').strip()
                branch = (repo.get('branch') or 'main').strip() or 'main'
                if url:
                    content = fetch_requirements_txt(request.env, url, branch)
                    if content:
                        result = merge_pip_requirements(
                            merged, content, url, branch,
                        )
                        merged = result['content']
            if merged != (project.pip_dependencies or ''):
                project.pip_dependencies = merged
        # ── Pip conflict detection ────────────────────────────────────────
        conflicts = detect_pip_conflicts(project.pip_dependencies or '')
        if conflicts:
            create_pip_conflict_alert(
                request.env, conflicts, project_id=project.id,
            )
        return {'id': project.id, 'name': project.name}

    @http.route(['/cloud/save_project'], type='jsonrpc', auth='user')
    def cloud_save_project(self, project_id, vals):
        self._sec()._check_cloud_group('group_cloud_consultant')
        project = request.env['cloud.project'].browse(project_id)
        if not project.exists():
            return {'ok': False, 'error': _('Project not found')}
        repos = vals.pop('repos', None)
        tag_ids = vals.pop('tag_ids', None)
        member_ids = vals.pop('member_ids', None)
        _PROJ_ALLOWED = {
            'name', 'description', 'project_author', 'project_license',
            'odoo_version', 'odoo_commit_sha', 'odoo_initial_lang',
            'pip_dependencies', 'apt_dependencies',
            'backup_backend_id',
        }
        safe_vals = {k: v for k, v in vals.items() if k in _PROJ_ALLOWED}
        if tag_ids is not None:
            safe_vals['tag_ids'] = [fields.Command.set(tag_ids)]
        if member_ids is not None:
            safe_vals['member_ids'] = [fields.Command.set(member_ids)]
        if safe_vals:
            project.write(safe_vals)
        if repos is not None:
            project.repo_ids.unlink()
            Repo = request.env['cloud.project.repo'].with_context(
                skip_apply_requirements=True,
            )
            for seq, repo in enumerate(repos, start=10):
                if repo.get('url', '').strip():
                    Repo.create({
                        'project_id': project.id,
                        'sequence': seq,
                        'url': repo['url'].strip(),
                        'branch': repo.get('branch', 'main').strip() or 'main',
                        'addons': repo.get('addons', '').strip(),
                        'excludes': repo.get('excludes', '').strip(),
                        'commit_sha': repo.get('commit_sha', '').strip(),
                    })
        # ── Pip conflict detection ────────────────────────────────────────────
        pip_deps = project.pip_dependencies or ''
        conflicts = detect_pip_conflicts(pip_deps)
        Alert = request.env['cloud.alert']
        if conflicts:
            create_pip_conflict_alert(
                request.env, conflicts, project_id=project.id,
            )
        else:
            active = Alert.search([
                ('code', '=', 'pip_conflict'),
                ('project_id', '=', project.id),
                ('state', '=', 'active'),
            ], limit=1)
            if active:
                active.write({'state': 'dismissed'})
        return {'ok': True}

    @http.route(['/cloud/get_host_instances'], type='jsonrpc', auth='user')
    def cloud_get_host_instances(self, host_id):
        host = request.env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}
        return {
            'host_name': host.name,
            'instances': [
                {
                    'id': i.id,
                    'name': i.name,
                    'environment': i.environment,
                    'status': i.status,
                    'deployed': i.deployed,
                    'running': i.running,
                    'host': i.host_id.name if i.host_id else '',
                    'host_id': i.host_id.id if i.host_id else None,
                    'host_ip': i.host_id.ip_address if i.host_id else '',
                    'host_user': i.host_id.user if i.host_id else '',
                    'host_port': i.host_id.port if i.host_id else 22,
                    'db_name': i.postgres_dbname or 'prod',
                    'domain': i.domain or '',
                    'compose_services': i.compose_services or 'odoo,db',
                    'project_id': i.project_id.id if i.project_id else None,
                    'project_name': i.project_id.name if i.project_id else '',
                    'pr_number': i.pr_number or 0,
                }
                for i in host.instance_ids
            ],
        }

    @http.route(['/cloud/get_project_instances'], type='jsonrpc', auth='user')
    def cloud_get_project_instances(self, project_id):
        project = request.env['cloud.project'].browse(project_id)
        if not project.exists():
            return {'ok': False, 'error': _('Project not found')}
        CloudJob = request.env['cloud.job']
        instances = []
        for i in project.instance_ids:
            latest = CloudJob.search(
                [('instance_id', '=', i.id)],
                order='create_date desc', limit=1,
            )
            instances.append({
                'id': i.id,
                'name': i.name,
                'environment': i.environment,
                'status': i.status,
                'deployed': i.deployed,
                'running': i.running,
                'host': i.host_id.name if i.host_id else '',
                'host_id': i.host_id.id if i.host_id else None,
                'host_ip': i.host_id.ip_address if i.host_id else '',
                'host_user': i.host_id.user if i.host_id else '',
                'host_port': i.host_id.port if i.host_id else 22,
                'db_name': i.postgres_dbname or 'prod',
                'domain': i.domain or '',
                'compose_services': i.compose_services or 'odoo,db',
                'latest_job_id': latest.id if latest else None,
                'pr_number': i.pr_number or 0,
            })
        return {
            'project_name': project.name,
            'instances': instances,
        }

    _CREATE_INSTANCE_ALLOWED = {
        'name', 'project_id', 'host_id', 'environment',
        'odoo_version', 'odoo_commit_sha',
        'odoo_initial_lang', 'odoo_admin_password', 'odoo_proxy',
        'postgres_version', 'postgres_dbname', 'postgres_username', 'postgres_password',
        'pip_dependencies', 'apt_dependencies',
        'smtp_relay_host', 'smtp_relay_port', 'smtp_relay_security',
        'smtp_relay_user', 'smtp_relay_password',
        'odoo_memory_limit', 'odoo_cpus', 'db_memory_limit', 'db_cpus',
        'backup_memory_limit', 'backup_cpus', 'smtp_memory_limit', 'smtp_cpus',
    }

    @http.route(['/cloud/create_instance'], type='jsonrpc', auth='user')
    def cloud_create_instance(self, vals):
        self._sec()._check_can_create_instance()
        repos = vals.pop('repos', None) if isinstance(vals, dict) else None
        domains = vals.pop('domains', None) if isinstance(vals, dict) else None
        safe = {k: v for k, v in vals.items() if k in self._CREATE_INSTANCE_ALLOWED}
        # If a single domain string was passed, convert to domain_ids
        domain_str = vals.get('domain', '').strip()
        if domain_str:
            safe['domain_ids'] = [(0, 0, {'hostname': domain_str})]
        elif domains:
            safe['domain_ids'] = [
                (0, 0, {
                    'hostname': (d.get('hostname') or '').strip(),
                    'redirect_to': (d.get('redirect_to') or '').strip(),
                    'cert_resolver': (
                        d.get('cert_resolver') or 'letsencrypt'
                    ).strip(),
                })
                for d in domains if (d.get('hostname') or '').strip()
            ]
        # ── Normalize odoo_version to string (frontend may send a number) ──
        if 'odoo_version' in safe:
            v = safe['odoo_version']
            if isinstance(v, (int, float)):
                safe['odoo_version'] = f"{float(v):.1f}"
        # ── Inherit odoo_version and odoo_initial_lang from project ──────────
        project_id = safe.get('project_id')
        if project_id:
            project = request.env['cloud.project'].browse(project_id)
            if project.exists():
                if project.odoo_version:
                    safe['odoo_version'] = project.odoo_version
                if project.odoo_initial_lang and 'odoo_initial_lang' not in safe:
                    safe['odoo_initial_lang'] = project.odoo_initial_lang.id
        # ── Block if project has unresolved pip conflicts ──────────────────
        if project_id:
            pip_alert = request.env['cloud.alert'].search([
                ('code', '=', 'pip_conflict'),
                ('project_id', '=', project_id),
                ('state', '=', 'active'),
            ], limit=1)
            if pip_alert:
                return {
                    'blocked': True,
                    'reason': 'pip_conflict',
                    'message': _(
                        'Project has unresolved pip dependency conflicts. '
                        'Resolve them in the project settings before '
                        'creating an instance.'
                    ),
                }
        # ── Auto-assign host when not explicitly provided ─────────────────
        if not safe.get('host_id'):
            autoassign = (
                request.env['ir.config_parameter'].sudo()
                .get_param('incubacloud.host_autoassign', '0') == '1'
            )
            if not autoassign:
                return {
                    'blocked': True,
                    'reason': 'host_required',
                    'message': _(
                        'Please select a host for the instance.'
                    ),
                }
            cpus, ram = (
                request.env['cloud.instance']
                ._compute_instance_resources(safe)
            )
            best_host = request.env['cloud.host'].select_best_host(cpus, ram)
            if not best_host:
                return {
                    'blocked': True,
                    'reason': 'no_host',
                    'message': _(
                        'No host with sufficient resources is available.'
                    ),
                }
            safe['host_id'] = best_host.id
        Instance = request.env['cloud.instance']
        if repos:
            Instance = Instance.with_context(skip_project_repos=True)
        instance = Instance.create(safe)
        if repos:
            # skip_apply_requirements: the frontend already merged requirements
            # via _fetchAndMergeRequirements on branch change; re-fetching here
            # would block the request with N synchronous GitHub API calls.
            Repo = request.env['cloud.instance.repo'].with_context(
                skip_apply_requirements=True,
            )
            for seq, repo in enumerate(repos, start=10):
                if (repo.get('url') or '').strip():
                    Repo.create({
                        'instance_id': instance.id,
                        'sequence': seq,
                        'url': repo['url'].strip(),
                        'branch': (
                            (repo.get('branch') or 'main').strip() or 'main'
                        ),
                        'addons': repo.get('addons') or '',
                        'excludes': repo.get('excludes') or '',
                        'commit_sha': repo.get('commit_sha') or '',
                    })
        return {'id': instance.id, 'name': instance.name}

    # ── General settings ──────────────────────────────────────────────────

    @http.route(['/cloud/get_general_settings'], type='jsonrpc', auth='user')
    def cloud_get_general_settings(self):
        ICP = request.env['ir.config_parameter'].sudo()
        bb_id = int(ICP.get_param(
            'incubacloud.backup_backend_id', 0,
        ) or 0)
        return {
            'autoassign_enabled': ICP.get_param(
                'incubacloud.host_autoassign', '0',
            ) == '1',
            'default_backup_backend_id': bb_id or None,
            'audit_log_retention_days': int(
                ICP.get_param('incubacloud.audit_log_retention_days', '90') or 90
            ),
        }

    @http.route(['/cloud/save_general_settings'], type='jsonrpc', auth='user')
    def cloud_save_general_settings(
        self, autoassign_enabled=False, default_backup_backend_id=None,
        audit_log_retention_days=90,
    ):
        self._sec()._check_can_manage_hosts()
        ICP = request.env['ir.config_parameter'].sudo()
        ICP.set_param(
            'incubacloud.host_autoassign',
            '1' if autoassign_enabled else '0',
        )
        ICP.set_param(
            'incubacloud.backup_backend_id',
            str(int(default_backup_backend_id))
            if default_backup_backend_id else '0',
        )
        ICP.set_param(
            'incubacloud.audit_log_retention_days',
            str(max(0, int(audit_log_retention_days or 0))),
        )
        return {'ok': True}

    @http.route(['/cloud/get_langs'], type='jsonrpc', auth='user')
    def cloud_get_langs(self):
        langs = request.env['res.lang'].with_context(active_test=False).search(
            [], order='name asc'
        )
        return [{'id': l.id, 'code': l.code, 'name': l.name} for l in langs]

    def _serialize_instance(self, inst):
        """Serialize a cloud.instance to a dict (shared by get_instance and get_project_full)."""
        Job = request.env['cloud.job']
        last_deploy = Job.search([
            ('instance_id', '=', inst.id),
            ('job_type_id.code', 'in',
             ('deploy_instance', 'rebuild_instance')),
            ('state', 'in', ('done', 'failed')),
        ], order='id desc', limit=1)
        hidden = Job._get_hidden_job_types()
        jobs = Job.search([
            ('instance_id', '=', inst.id),
            ('job_type_id.code', 'not in', hidden),
        ], order='id desc', limit=5)
        jobs_total = Job.search_count([
            ('instance_id', '=', inst.id),
            ('job_type_id.code', 'not in', hidden),
        ])
        return {
            'id': inst.id,
            'name': inst.name,
            'environment': inst.environment,
            'status': inst.status,
            'deployed': inst.deployed,
            'running': inst.running,
            'auto_rebuild': inst.auto_rebuild,
            'host_id': inst.host_id.id if inst.host_id else None,
            'host': inst.host_id.name if inst.host_id else '',
            'host_ip': inst.host_id.ip_address if inst.host_id else '',
            'host_user': inst.host_id.user if inst.host_id else '',
            'host_port': inst.host_id.port if inst.host_id else 22,
            'odoo_version': inst.odoo_version,
            'odoo_commit_sha': inst.odoo_commit_sha or '',
            'last_deploy_date': last_deploy.create_date if last_deploy else None,
            'last_deploy_state': last_deploy.state if last_deploy else None,
            'odoo_initial_lang': inst.odoo_initial_lang.id if inst.odoo_initial_lang else None,
            'has_odoo_admin_password': bool(inst.odoo_admin_password),
            'odoo_proxy': inst.odoo_proxy or 'traefik',
            'postgres_version': inst.postgres_version or '17',
            'postgres_dbname': inst.postgres_dbname or '',
            'postgres_username': inst.postgres_username or '',
            'has_postgres_password': bool(inst.postgres_password),
            'domain': inst.domain or '',
            'smtp_relay_host': inst.smtp_relay_host or '',
            'smtp_relay_port': inst.smtp_relay_port or 587,
            'smtp_relay_security': inst.smtp_relay_security or 'starttls',
            'smtp_relay_user': inst.smtp_relay_user or '',
            'has_smtp_relay_password': bool(inst.smtp_relay_password),
            'odoo_conf': inst.odoo_conf or '',
            'odoo_memory_limit': inst.odoo_memory_limit or '',
            'odoo_cpus': inst.odoo_cpus or 0,
            'db_memory_limit': inst.db_memory_limit or '',
            'db_cpus': inst.db_cpus or 0,
            'backup_memory_limit': inst.backup_memory_limit or '',
            'backup_cpus': inst.backup_cpus or 0,
            'smtp_memory_limit': inst.smtp_memory_limit or '',
            'smtp_cpus': inst.smtp_cpus or 0,
            'pip_dependencies': inst.pip_dependencies or '',
            'apt_dependencies': inst.apt_dependencies or '',
            'compose_services': (inst.compose_services or 'odoo,db').split(','),
            'backup_backend_id': (
                inst.backup_backend_id.id if inst.backup_backend_id else None
            ),
            'effective_backup_backend_id': (
                inst.effective_backup_backend.id
                if inst.effective_backup_backend else None
            ),
            'effective_backup_backend_name': (
                inst.effective_backup_backend.name
                if inst.effective_backup_backend else ''
            ),
            'domains': [
                {
                    'id': d.id,
                    'hostname': d.hostname or '',
                    'redirect_to': d.redirect_to or '',
                    'cert_resolver': d.cert_resolver or 'letsencrypt',
                }
                for d in inst.domain_ids
            ],
            'repos': [
                {
                    'id': r.id,
                    'url': r.url or '',
                    'branch': r.branch or 'main',
                    'addons': r.addons or '',
                    'excludes': r.excludes or '',
                    'commit_sha': r.commit_sha or '',
                }
                for r in inst.repo_ids
            ],
            'jobs': jobs._format(),
            'jobsTotal': jobs_total,
            'custom_actions': [
                {
                    'code': jt.code,
                    'name': jt.name,
                    'action_icon': jt.action_icon or 'fa-cog',
                }
                for jt in request.env['cloud.job.type'].search([
                    ('show_as_action', '=', True),
                    ('apply_to', '=', 'instance'),
                ], order='action_order, id')
            ],
        }

    @http.route(['/cloud/get_instance'], type='jsonrpc', auth='user')
    def cloud_get_instance(self, instance_id):
        inst = request.env['cloud.instance'].browse(instance_id)
        if not inst.exists():
            return {'ok': False, 'error': _('Instance not found')}
        return self._serialize_instance(inst)

    @http.route(['/cloud/get_project_full'], type='jsonrpc', auth='user')
    def cloud_get_project_full(self, project_id):
        """Load entire project data in one RPC for instant switching."""
        project = request.env['cloud.project'].browse(project_id)
        if not project.exists():
            return {'ok': False, 'error': _('Project not found')}
        instances = {}
        for inst in project.instance_ids:
            instances[inst.id] = self._serialize_instance(inst)
        hosts = request.env['cloud.host'].search([])
        backends = request.env['cloud.backup.backend'].search([])
        return {
            'project': {
                'id': project.id,
                'name': project.name,
                'odoo_version': project.odoo_version or '19.0',
            },
            'instances': instances,
            'hosts': [{
                'id': h.id, 'name': h.name,
                'status': h.status,
                'traefik_deployed': h.traefik_deployed,
                'available_cpus': round(h.available_cpus, 2),
                'available_ram_gb': round(h.available_ram_gb, 2),
                'cpu_cores': h.cpu_cores,
                'ram_total_gb': h.ram_total_gb,
            } for h in hosts],
            'backup_backends': [{
                'id': b.id, 'name': b.name,
                'backup_dst': b.backup_dst or '',
            } for b in backends],
        }

    _SAVE_INSTANCE_ALLOWED = {
        'name', 'environment', 'host_id',
        'odoo_version', 'odoo_commit_sha',
        'odoo_initial_lang', 'odoo_admin_password',
        'odoo_proxy',
        'postgres_version', 'postgres_dbname', 'postgres_username',
        'postgres_password',
        'smtp_relay_host', 'smtp_relay_port', 'smtp_relay_security',
        'smtp_relay_user', 'smtp_relay_password',
        'odoo_conf', 'pip_dependencies', 'apt_dependencies',
        'backup_backend_id', 'auto_rebuild',
        'odoo_memory_limit', 'odoo_cpus',
        'db_memory_limit', 'db_cpus',
        'backup_memory_limit', 'backup_cpus',
        'smtp_memory_limit', 'smtp_cpus',
    }

    @http.route(['/cloud/save_instance'], type='jsonrpc', auth='user')
    def cloud_save_instance(self, instance_id, vals):
        self._sec()._check_cloud_group('group_cloud_consultant')
        inst = request.env['cloud.instance'].browse(instance_id)
        if not inst.exists():
            return {'ok': False, 'error': _('Instance not found')}
        repos = vals.pop('repos', None)
        domains = vals.pop('domains', None)
        safe = {
            k: v for k, v in vals.items()
            if k in self._SAVE_INSTANCE_ALLOWED
        }
        if 'odoo_version' in safe:
            v = safe['odoo_version']
            if isinstance(v, (int, float)):
                safe['odoo_version'] = f"{float(v):.1f}"
        # Track whether host_id is changing (auto-domain may fire in write)
        host_changed = 'host_id' in safe and safe['host_id'] != (
            inst.host_id.id if inst.host_id else None
        )
        inst.write(safe)
        # Sync domains — skip if host just changed and frontend sent
        # empty domains, because write() auto-generated them.
        if domains is not None and not (
            host_changed and not domains and inst.domain_ids
        ):
            Domain = request.env['cloud.instance.domain']
            existing_ids = set(inst.domain_ids.ids)
            kept_ids = set()
            for d in domains:
                did = d.get('id')
                hostname = (d.get('hostname') or '').strip()
                if not hostname:
                    continue
                dvals = {
                    'hostname': hostname,
                    'redirect_to': (d.get('redirect_to') or '').strip(),
                    'cert_resolver': (
                        d.get('cert_resolver') or 'letsencrypt'
                    ).strip(),
                }
                if did and did in existing_ids:
                    Domain.browse(did).write(dvals)
                    kept_ids.add(did)
                else:
                    new = Domain.create({
                        **dvals, 'instance_id': inst.id,
                    })
                    kept_ids.add(new.id)
            to_delete = existing_ids - kept_ids
            if to_delete:
                Domain.browse(list(to_delete)).unlink()
        if repos is not None:
            # Skip GitHub fetches: pip_dependencies is already merged by the
            # frontend before saving (via /cloud/get_repo_requirements).
            Repo = request.env['cloud.instance.repo'].with_context(
                skip_apply_requirements=True,
            )
            existing_ids = set(inst.repo_ids.ids)
            kept_ids = set()
            for repo in repos:
                rid = repo.get('id')
                url = (repo.get('url') or '').strip()
                if not url:
                    continue
                branch = (repo.get('branch') or 'main').strip() or 'main'
                addons = (repo.get('addons') or '').strip()
                excludes = (repo.get('excludes') or '').strip()
                commit_sha = (repo.get('commit_sha') or '').strip()
                if rid and rid in existing_ids:
                    Repo.browse(rid).write({
                        'url': url,
                        'branch': branch,
                        'addons': addons,
                        'excludes': excludes,
                        'commit_sha': commit_sha,
                    })
                    kept_ids.add(rid)
                else:
                    Repo.create({
                        'instance_id': inst.id,
                        'url': url,
                        'branch': branch,
                        'addons': addons,
                        'excludes': excludes,
                        'commit_sha': commit_sha,
                    })
            to_unlink = existing_ids - kept_ids
            if to_unlink:
                Repo.browse(list(to_unlink)).unlink()
        return {'ok': True}

    # ── GitHub App endpoints ─────────────────────────────────────────────────

    @http.route(['/cloud/get_github_app'], type='jsonrpc', auth='user')
    def cloud_get_github_app(self):
        app = request.env['cloud.github.app'].sudo().search([], limit=1)
        if not app:
            return {
                'configured': False,
                'app_id': '',
                'installation_id': '',
                'webhook_secret': '',
                'has_private_key': False,
                'has_pat': _has_pat(request.env),
            }
        slug = app.slug or ''
        return {
            'configured': True,
            'app_id': app.app_id or '',
            'installation_id': app.installation_id or '',
            'webhook_secret': app.webhook_secret or '',
            'has_private_key': bool(app.sudo().private_key),
            'has_pat': _has_pat(request.env),
            'slug': slug,
            'install_url': f'https://github.com/apps/{slug}/installations/new' if slug else '',
        }

    @http.route(['/cloud/save_github_app'], type='jsonrpc', auth='user')
    def cloud_save_github_app(self, vals):
        self._sec()._check_can_manage_settings()
        App = request.env['cloud.github.app'].sudo()
        app_id = (vals.get('app_id') or '').strip()
        installation_id = (vals.get('installation_id') or '').strip()
        private_key = (vals.get('private_key') or '').strip()
        webhook_secret = (vals.get('webhook_secret') or '').strip()

        if not app_id:
            return {'ok': False, 'error': _('App ID is required.')}

        github_pat = (vals.get('github_pat') or '').strip()

        write_vals = {
            'app_id': app_id,
            'installation_id': installation_id,
            'webhook_secret': webhook_secret,
        }
        if private_key:
            write_vals['private_key'] = private_key

        app = App.search([], limit=1)
        if app:
            app.write(write_vals)
        else:
            if not private_key:
                return {
                    'ok': False,
                    'error': _('Private key is required '
                               'for the initial setup.'),
                }
            App.create(write_vals)

        # PAT stored in cloud.settings (EncryptedChar)
        if github_pat:
            request.env['cloud.settings']._get().write(
                {'github_pat': github_pat},
            )

        return {'ok': True}

    @http.route(['/cloud/save_github_pat'], type='jsonrpc', auth='user')
    def cloud_save_github_pat(self, pat):
        """Save PAT independently of the GitHub App."""
        self._sec()._check_can_manage_settings()
        pat = (pat or '').strip()
        if not pat:
            return {'ok': False, 'error': _('PAT is empty.')}
        request.env['cloud.settings']._get().write({'github_pat': pat})
        return {'ok': True}

    @http.route(['/cloud/test_github_connection'], type='jsonrpc', auth='user')
    def cloud_test_github_connection(self):
        self._sec()._check_can_manage_settings()
        return request.env['cloud.github.credential.service'].sudo().test_connection()

    @http.route(['/cloud/reset_github_app'], type='jsonrpc', auth='user')
    def cloud_reset_github_app(self):
        """Delete the GitHub App configuration, allowing a fresh setup."""
        self._sec()._check_can_manage_settings()
        app = request.env['cloud.github.app'].sudo().search([], limit=1)
        if app:
            app.unlink()
        return {'ok': True}

    @http.route(['/cloud/detect_github_installation'], type='jsonrpc', auth='user')
    def cloud_detect_github_installation(self):
        """List active installations via JWT and auto-update installation_id if exactly one."""
        self._sec()._check_can_manage_settings()
        try:
            svc = request.env['cloud.github.credential.service'].sudo()
            creds = svc.get_credentials()
            client = GitHubAppClient(creds)
            installations = client.list_installations()
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

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

    # ────────────────────────────────────────────────────────────────────────

    @http.route(['/cloud/deploy_instance'], type='jsonrpc', auth='user')
    def cloud_deploy_instance(self, instance_id):
        self._sec()._check_can_deploy()
        instance = request.env['cloud.instance'].browse(instance_id)
        if not instance.exists():
            return {'ok': False, 'error': _('Instance not found')}
        running_job = request.env['cloud.job'].search([
            ('instance_id', '=', instance.id),
            ('state', 'in', ('started', 'pending', 'enqueued')),
        ], limit=1)
        if running_job:
            return {'ok': False, 'error': _('A job is already running: %s. Wait for it to complete or cancel it first.') % running_job.name}
        try:
            job_id = instance.deploy()
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        return _job_response(request.env, job_id)

    @http.route(['/cloud/rebuild_instance'], type='jsonrpc', auth='user')
    def cloud_rebuild_instance(self, instance_id):
        self._sec()._check_can_deploy()
        instance = request.env['cloud.instance'].browse(instance_id)
        if not instance.exists():
            return {'ok': False, 'error': _('Instance not found')}
        if not instance.host_id:
            return {'ok': False, 'error': _('Instance has no host configured')}
        running_job = request.env['cloud.job'].search([
            ('instance_id', '=', instance.id),
            ('state', 'in', ('started', 'pending', 'enqueued')),
        ], limit=1)
        if running_job:
            return {'ok': False, 'error': _('A job is already running: %s. Wait for it to complete or cancel it first.') % running_job.name}
        job_id = request.env['cloud.job'].enqueue(
            instance.host_id.id, instance_id, 'rebuild_instance',
        )
        return _job_response(request.env, job_id)

    # ── Backup management ──────────────────────────────────────────────────

    def _serialize_backups(self, instance_id):
        """Return backup records for an instance as a dict."""
        backups = request.env['cloud.instance.backup'].sudo().search([
            ('instance_id', '=', instance_id),
        ], order='backup_time desc')
        return {
            'backups': [{
                'id': b.id,
                'type': b.backup_type,
                'time': b.backup_time.isoformat() if b.backup_time else '',
                'volumes': b.volumes,
                'is_primary': b.is_primary,
                'chain_start': (
                    b.chain_start.isoformat() if b.chain_start else None
                ),
                'attachment_id': b.attachment_id.id if b.attachment_id else None,
                'size': b.attachment_id.file_size if b.attachment_id else None,
            } for b in backups],
        }

    @http.route(['/cloud/list_backups'], type='jsonrpc', auth='user')
    def cloud_list_backups(self, instance_id, refresh=False):
        """List backups from cloud.instance.backup records.

        refresh=True enqueues a backup_list job (production: SSH scan,
        non-production: no-op that reports existing records).
        """
        self._sec()._check_can_manage_backups()
        instance = request.env['cloud.instance'].browse(instance_id)
        if not instance.exists():
            return {'ok': False, 'error': _('Instance not found')}

        if refresh:
            if not instance.host_id:
                return {'ok': False, 'error': _('No host configured')}
            job_id = instance.list_backups()
            return {'ok': True, 'job_id': job_id}

        return {
            'ok': True, 'state': 'done',
            'result': self._serialize_backups(instance_id),
        }

    @http.route(['/cloud/get_backup_result'], type='jsonrpc', auth='user')
    def cloud_get_backup_result(self, job_id):
        """Poll a backup_list job; return persisted records when done."""
        self._sec()._check_can_manage_backups()
        job = request.env['cloud.job'].browse(job_id)
        if not job.exists():
            return {'ok': False, 'error': _('Job not found')}
        state = 'blocked' if job.blocked_alert_id else job.state
        if state in ('pending', 'enqueued', 'started', 'wait_dependencies'):
            return {'ok': True, 'state': state, 'result': None}
        if state == 'done':
            inst_id = job.instance_id.id if job.instance_id else None
            return {
                'ok': True, 'state': 'done',
                'result': self._serialize_backups(inst_id) if inst_id else {},
            }
        return {
            'ok': False,
            'state': state,
            'error': job._get_last_system_message() or _('Job failed.'),
        }

    @http.route(['/cloud/create_backup'], type='jsonrpc', auth='user')
    def cloud_create_backup(self, instance_id):
        """Enqueue a backup_create job."""
        self._sec()._check_can_manage_backups()
        instance = request.env['cloud.instance'].browse(instance_id)
        if not instance.exists():
            return {'ok': False, 'error': _('Instance not found')}
        if not instance.host_id:
            return {'ok': False, 'error': _('Instance has no host configured')}
        if not instance.deployed:
            return {'ok': False, 'error': _('Instance is not deployed')}
        job_id = instance.create_backup()
        return {'ok': True, 'job_id': job_id}

    @http.route(['/cloud/clone_to_staging'], type='jsonrpc', auth='user')
    def cloud_clone_to_staging(self, instance_id, staging_name):
        self._sec()._check_can_clone_to_staging()
        instance = request.env['cloud.instance'].browse(instance_id)
        if not instance.exists():
            return {'ok': False, 'error': _('Instance not found')}
        if instance.environment != 'production':
            return {'ok': False, 'error': _('Only production instances')}
        if not instance.deployed:
            return {'ok': False, 'error': _('Instance not deployed')}
        if not staging_name or not staging_name.strip():
            return {'ok': False, 'error': _('Name is required')}
        result = instance.clone_to_staging(staging_name.strip())
        return {'ok': True, **result}

    @http.route(['/cloud/download_backup'], type='jsonrpc', auth='user')
    def cloud_download_backup(self, instance_id, time, download_type='dump'):
        """Download a backup. Prod: enqueue job. Non-prod: return attachment URL."""
        self._sec()._check_can_manage_backups()
        instance = request.env['cloud.instance'].browse(instance_id)
        if not instance.exists():
            return {'ok': False, 'error': _('Instance not found')}
        if not instance.host_id:
            return {'ok': False, 'error': _('Instance has no host configured')}
        job_id = instance.download_backup({
            'time': time,
            'download_type': download_type,
        })
        return {'ok': True, 'job_id': job_id}

    @http.route(['/cloud/restore_backup'], type='jsonrpc', auth='user')
    def cloud_restore_backup(self, instance_id, time):
        """Restore a production instance from a duplicity backup."""
        self._sec()._check_can_manage_backups()
        instance = request.env['cloud.instance'].browse(instance_id)
        if not instance.exists():
            return {'ok': False, 'error': _('Instance not found')}
        if instance.environment != 'production':
            return {
                'ok': False,
                'error': _('Backup restore only supported for production.'),
            }
        if not instance.host_id:
            return {'ok': False, 'error': _('Instance has no host configured')}
        job_id = instance.restore_backup({'time': time})
        return {'ok': True, 'job_id': job_id}

    @http.route(['/cloud/delete_project'], type='jsonrpc', auth='user')
    def cloud_delete_project(self, project_id):
        self._sec()._check_can_delete_project()
        project = request.env['cloud.project'].browse(project_id)
        if not project.exists():
            return {'ok': False, 'error': _('Project not found')}
        count = len(project.instance_ids)
        if count:
            return {
                'ok': False,
                'error': _(
                    'This project has %(count)s instance(s). '
                    'Delete all instances before deleting the project.'
                ) % {'count': count},
            }
        project.unlink()
        return {'ok': True}

    @http.route(['/cloud/delete_instance'], type='jsonrpc', auth='user')
    def cloud_delete_instance(self, instance_id):
        inst = request.env['cloud.instance'].browse(instance_id)
        if not inst.exists():
            return {'ok': False, 'error': _('Instance not found')}
        self._sec()._check_can_delete_instance(inst)
        running_job = request.env['cloud.job'].search([
            ('instance_id', '=', inst.id),
            ('state', 'in', ('started', 'pending', 'enqueued')),
        ], limit=1)
        if running_job:
            return {
                'ok': False,
                'error': _(
                    'Cannot delete: a job is running (%s). '
                    'Wait for it to complete or cancel it first.'
                ) % running_job.name,
            }
        inst.unlink()
        return {'ok': True}

    # ── GitHub repo introspection ─────────────────────────────────────────────

    @http.route(['/cloud/get_repo_branches'], type='jsonrpc', auth='user')
    def cloud_get_repo_branches(self, url):
        """Return branch names for a GitHub repo. Tries App first, then PAT."""

        def _fetch(client):
            owner, repo = _parse_github_repo_path(url)
            branches, page = [], 1
            while True:
                page_data = client.get(
                    f'/repos/{owner}/{repo}/branches?per_page=100&page={page}'
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
            pass  # app_status stays not_configured
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
        return {'ok': False, 'error': msg, 'branches': []}

    @staticmethod
    def _build_github_error(app_status, pat_status, has_pat):
        """Build a user-friendly error based on what was tried."""
        if app_status == 'not_configured' and not has_pat:
            return _(
                "No GitHub credentials configured. "
                "Go to Settings → GitHub to set up a GitHub App "
                "or configure a Personal Access Token (PAT)."
            )
        if app_status == 'not_configured' and pat_status == 'no_access':
            return _(
                "PAT does not have access to this repository. "
                "Verify the token has the 'repo' scope "
                "or set up a GitHub App in Settings → GitHub."
            )
        if app_status == 'no_access' and not has_pat:
            return _(
                "GitHub App does not have access to this repository. "
                "Install the App on the repository's organization "
                "or configure a PAT in Settings → GitHub."
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
            "and your GitHub credentials in Settings."
        )

    @http.route(['/cloud/get_branch_head'], type='jsonrpc', auth='user')
    def cloud_get_branch_head(self, url, branch):
        """Return the HEAD commit SHA of a branch (for freeze)."""

        def _fetch(client):
            owner, repo = _parse_github_repo_path(url)
            data = client.get(
                f'/repos/{owner}/{repo}/branches/{branch}'
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
                return {'ok': False, 'error': str(exc)}
        except (ValueError, UserError):
            pass
        except Exception:
            pass

        # 2. Fall back to PAT
        try:
            pat = svc.get_pat()
            if pat:
                return {
                    'ok': True,
                    'sha': _fetch(GitHubPATClient(pat)),
                }
        except (GitHubAPIError, ValueError, UserError) as exc:
            return {'ok': False, 'error': str(exc)}
        except Exception:
            _logger.exception(
                "Error fetching branch HEAD for %s@%s",
                url, branch,
            )

        return {'ok': False, 'error': _('Unexpected error')}

    @http.route(['/cloud/freeze_repo'], type='jsonrpc', auth='user')
    def cloud_freeze_repo(self, repo_id, model):
        """Freeze a repo to its current branch HEAD. Writes commit_sha to DB."""
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

    @http.route(['/cloud/unfreeze_repo'], type='jsonrpc', auth='user')
    def cloud_unfreeze_repo(self, repo_id, model):
        """Unfreeze a repo — clears commit_sha so rebuilds use branch tip."""
        if model not in ('cloud.project.repo', 'cloud.instance.repo'):
            return {'ok': False, 'error': _('Invalid model')}
        repo = request.env[model].browse(repo_id)
        if not repo.exists():
            return {'ok': False, 'error': _('Repo not found')}
        repo.write({'commit_sha': False})
        return {'ok': True}

    @http.route(['/cloud/freeze_all_repos'], type='jsonrpc', auth='user')
    def cloud_freeze_all_repos(self, repo_ids, model):
        """Freeze multiple repos at once."""
        if model not in ('cloud.project.repo', 'cloud.instance.repo'):
            return {'ok': False, 'error': _('Invalid model')}
        results = {}
        for rid in repo_ids:
            repo = request.env[model].browse(rid)
            if not repo.exists() or not repo.url or not repo.branch or repo.commit_sha:
                continue
            res = self.cloud_get_branch_head(repo.url, repo.branch)
            if res.get('ok'):
                repo.write({'commit_sha': res['sha']})
                results[rid] = res['sha']
        return {'ok': True, 'results': results}

    @http.route(['/cloud/unfreeze_all_repos'], type='jsonrpc', auth='user')
    def cloud_unfreeze_all_repos(self, repo_ids, model):
        """Unfreeze multiple repos at once."""
        if model not in ('cloud.project.repo', 'cloud.instance.repo'):
            return {'ok': False, 'error': _('Invalid model')}
        repos = request.env[model].browse(repo_ids)
        repos.filtered('commit_sha').write({'commit_sha': False})
        return {'ok': True}

    @http.route(['/cloud/get_repo_modules'], type='jsonrpc', auth='user')
    def cloud_get_repo_modules(self, url, branch):
        """Return Odoo addon module names found at the root of a branch."""

        def _fetch(client):
            owner, repo = _parse_github_repo_path(url)
            tree = client.get(
                f'/repos/{owner}/{repo}/git/trees/{branch}?recursive=1'
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
                return {'ok': False, 'error': str(exc), 'modules': []}
        except (ValueError, UserError) as exc:
            return {'ok': False, 'error': str(exc), 'modules': []}
        except Exception:
            pass

        # 2. Fall back to GitHub App token
        try:
            return {'ok': True, 'modules': _fetch(GitHubAppClient(svc.get_credentials()))}
        except (ValueError, UserError) as exc:
            return {'ok': False, 'error': str(exc), 'modules': []}
        except GitHubAPIError as exc:
            return {'ok': False, 'error': str(exc), 'modules': []}
        except Exception:
            _logger.exception("Error fetching modules for %s@%s", url, branch)
            return {'ok': False, 'error': _('Unexpected error'), 'modules': []}

    @http.route(['/cloud/get_repo_requirements'], type='jsonrpc', auth='user')
    def cloud_get_repo_requirements(self, url, branch):
        """Fetch requirements.txt from the root of a GitHub repo branch."""

        try:
            owner, repo_name = _parse_github_repo_path(url)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc), 'content': '', 'found': False}

        def _fetch(client):
            data = client.get(
                f'/repos/{owner}/{repo_name}'
                f'/contents/requirements.txt?ref={branch}'
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
                return {'ok': False, 'error': str(exc), 'content': '', 'found': False}
        except (ValueError, UserError):
            pass
        except Exception:
            pass

        # 2. Try GitHub App token
        try:
            content = _fetch(GitHubAppClient(svc.get_credentials()))
            return {'ok': True, 'found': True, 'content': content}
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return {'ok': True, 'found': False, 'content': ''}
            return {'ok': False, 'error': str(exc), 'content': '', 'found': False}
        except (ValueError, UserError):
            pass
        except Exception:
            pass

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
                raw = data.get('content', '').replace('\n', '').replace(' ', '')
                content = base64.b64decode(raw).decode('utf-8')
                return {'ok': True, 'found': True, 'content': content}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {'ok': True, 'found': False, 'content': ''}
        except Exception:
            pass

        return {'ok': True, 'found': False, 'content': ''}

    # ── odoo.sh import wizard ────────────────────────────────────────────────

    @http.route(['/cloud/fetch_odoojs_submodules'], type='jsonrpc', auth='user')
    def cloud_fetch_odoojs_submodules(self, repo_url, branch='main'):
        """Clone a GitHub repo (minimal) to discover submodules and pinned SHAs.

        Uses ``git clone --depth 1 --no-checkout --filter=blob:none`` to
        download the absolute minimum, then reads ``.gitmodules`` and
        submodule SHAs from the git tree objects directly.
        """

        _CLONE_TIMEOUT = 30  # seconds

        try:
            _owner, repo_name = _parse_github_repo_path(repo_url)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

        # Detect Odoo version early — used as branch fallback for submodules
        _ver_match = re.fullmatch(r'(\d+\.\d)', branch)
        _SUPPORTED = {
            '7.0', '8.0', '9.0', '10.0', '11.0', '12.0',
            '13.0', '14.0', '15.0', '16.0', '17.0', '18.0', '19.0',
        }
        odoo_version = (
            _ver_match.group(1)
            if _ver_match and _ver_match.group(1) in _SUPPORTED
            else ''
        )

        # ── Helpers ────────────────────────────────────────────────────────────

        def _resolve_url(sub_url):
            """Resolve a potentially relative submodule URL to absolute HTTPS."""
            sub_url = sub_url.strip()
            if sub_url.startswith(('https://', 'http://', 'git@', 'ssh://')):
                return sub_url
            parsed = urlparse(repo_url.rstrip('/'))
            if parsed.path.endswith('.git'):
                parsed = parsed._replace(path=parsed.path[:-4])
            new_path = posixpath.normpath(posixpath.join(parsed.path, sub_url))
            return urlunparse(parsed._replace(path=new_path, query='', fragment=''))

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
            # First pass: collect explicit branches and infer odoo_version
            nonlocal odoo_version
            if not odoo_version:
                for s in submodules:
                    sb = s.get('branch', '')
                    if sb and sb != '.' and re.fullmatch(r'\d+\.\d', sb):
                        odoo_version = sb
                        break
            result = []
            for s in submodules:
                if 'url' not in s:
                    continue
                sub_branch = s.get('branch', '')
                if not sub_branch or sub_branch == '.':
                    sub_branch = odoo_version or branch
                result.append({
                    'url': _resolve_url(s['url']),
                    'branch': sub_branch,
                    'path': s.get('path', ''),
                })
            return result

        def _build_clone_url(token=None):
            """Build HTTPS clone URL, optionally with auth token."""
            url = repo_url.strip()
            if not url.startswith(('https://', 'http://')):
                url = f'https://{url}'
            parsed = urlparse(url)
            host = parsed.hostname or 'github.com'
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

    @http.route(['/cloud/import_project'], type='jsonrpc', auth='user')
    def cloud_import_project(self, url, branch='main'):
        """Import a GitHub repo as project + production instance.

        Detects repo type (doodba / odoo.sh / simple), parses config,
        creates project with repos, then creates a production instance
        inheriting project defaults + type-specific config.
        """

        self._sec()._check_can_create_instance()

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
            return {'ok': False, 'error': str(exc)}

        _SUPPORTED_VERSIONS = {
            '7.0', '8.0', '9.0', '10.0', '11.0', '12.0',
            '13.0', '14.0', '15.0', '16.0', '17.0', '18.0', '19.0',
        }

        def _detect_odoo_version_from_manifests(tmpdir):
            """Scan for __manifest__.py files and extract Odoo version.

            The ``version`` field in an Odoo manifest typically looks like
            ``"16.0.1.0.0"`` where the first two segments are the Odoo
            version.  We find the first manifest, parse it safely with
            ``ast.literal_eval``, and return the version string.
            """
            for root, _dirs, files in os.walk(tmpdir):
                if '__manifest__.py' in files:
                    fpath = os.path.join(root, '__manifest__.py')
                    try:
                        with open(fpath) as f:
                            data = ast.literal_eval(f.read())
                        ver = str(data.get('version', ''))
                        # "16.0.1.0.0" → "16.0"
                        parts = ver.split('.')
                        if len(parts) >= 2:
                            candidate = f'{parts[0]}.{parts[1]}'
                            if candidate in _SUPPORTED_VERSIONS:
                                return candidate
                    except Exception:
                        continue
            return ''

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
            host = parsed.hostname or 'github.com'
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

                    # Detect odoo_version from branch
                    vm = re.fullmatch(r'(\d+\.\d)', branch)
                    odoo_version = vm.group(1) if vm else ''

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
                    # Infer odoo_version from submodule branches
                    if not odoo_version:
                        for s in gm_subs:
                            sb = s.get('branch', '')
                            if sb and re.fullmatch(r'\d+\.\d', sb):
                                odoo_version = sb
                                break
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

                    for s in gm_subs:
                        sub_url = s.get('url', '')
                        if not sub_url:
                            continue
                        sub_url = _ssh_to_https(sub_url)
                        sub_path = s.get('path', '')
                        sub_branch = s.get('branch', '')
                        if not sub_branch or sub_branch == '.':
                            sub_branch = odoo_version or 'main'
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

                # Fallback: detect Odoo version from __manifest__.py
                if not odoo_version:
                    odoo_version = (
                        _detect_odoo_version_from_manifests(tmpdir)
                    )

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

    # ── Host instance import (file browser + import) ─────────────────────────

    def _ssh_run(self, host, command):
        """Run a single SSH command on a host synchronously."""

        async def _run():
            async with asyncssh.connect(
                **host.ssh_connect_kwargs(),
            ) as conn:
                result = await conn.run(command, check=False)
                return result.stdout or '', result.stderr or ''

        return run_async(_run())

    def _ssh_run_multi(self, host, commands):
        """Run multiple SSH commands on a host in one connection."""

        async def _run():
            results = []
            async with asyncssh.connect(
                **host.ssh_connect_kwargs(),
            ) as conn:
                for cmd in commands:
                    result = await conn.run(cmd, check=False)
                    results.append(
                        (result.stdout or '', result.stderr or ''),
                    )
            return results

        return run_async(_run())

    @http.route(['/cloud/browse_host_dir'], type='jsonrpc', auth='user')
    def cloud_browse_host_dir(self, host_id, path='~'):
        """Browse directories on a remote host via SSH.

        Returns directory listing with doodba instance detection.
        """
        self._sec()._check_can_manage_hosts()
        host = request.env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}

        # Resolve ~ and get listing
        try:
            stdout, _ = self._ssh_run(host, (
                f'cd {path} 2>/dev/null && pwd && echo "---" && '
                f'ls -1pA 2>/dev/null'
            ))
        except Exception as exc:
            _logger.exception("SSH browse failed for host %s", host_id)
            return {'ok': False, 'error': str(exc)}

        lines = stdout.strip().splitlines()
        if not lines or '---' not in lines:
            return {'ok': False, 'error': _('Could not list directory')}

        sep_idx = lines.index('---')
        current_path = lines[0].strip()
        entries_raw = lines[sep_idx + 1:]

        # Identify directories (ls -p appends / to dirs)
        dirs = []
        for entry in entries_raw:
            entry = entry.strip()
            if entry.endswith('/'):
                name = entry.rstrip('/')
                if name not in ('.', '..'):
                    dirs.append(name)

        # Check which dirs are doodba instances
        # (must have BOTH .copier-answers.yml AND docker-compose.yml)
        if dirs:
            checks = ' '.join(
                f'([ -f "{current_path}/{d}/.copier-answers.yml" ] '
                f'&& [ -f "{current_path}/{d}/docker-compose.yml" ] '
                f'&& echo "DOODBA:{d}") || true'
                for d in dirs[:50]
            )
            try:
                check_stdout, _ = self._ssh_run(host, checks)
            except Exception:
                check_stdout = ''
            doodba_dirs = set()
            for line in check_stdout.splitlines():
                if line.startswith('DOODBA:'):
                    doodba_dirs.add(line[7:].strip())
        else:
            doodba_dirs = set()

        # Check if current directory is a doodba instance
        is_current_doodba = False
        copier_info = {}
        with suppress(Exception):
            stdout_check, _ = self._ssh_run(host, (
                f'[ -f "{current_path}/.copier-answers.yml" ] && '
                f'[ -f "{current_path}/docker-compose.yml" ] && '
                f'cat "{current_path}/.copier-answers.yml" || echo ""'
            ))
            if stdout_check.strip() and 'odoo_version' in stdout_check:
                is_current_doodba = True
                data = yaml.safe_load(stdout_check) or {}
                copier_info = {
                    'project_name': data.get('project_name', ''),
                    'odoo_version': str(data.get('odoo_version', '')),
                }

        # Check already imported instances
        imported_paths = set(
            request.env['cloud.instance'].search([
                ('host_id', '=', host_id),
                ('custom_remote_dir', '!=', False),
            ]).mapped('custom_remote_dir')
        )

        parent_path = '/'.join(current_path.rstrip('/').split('/')[:-1]) or '/'

        entries = []
        for d in sorted(dirs):
            full_path = f"{current_path}/{d}"
            entries.append({
                'name': d,
                'is_dir': True,
                'is_doodba': d in doodba_dirs,
                'already_imported': full_path in imported_paths,
            })

        return {
            'ok': True,
            'current_path': current_path,
            'parent_path': parent_path,
            'is_doodba': is_current_doodba,
            'copier_info': copier_info,
            'already_imported': current_path in imported_paths,
            'entries': entries,
        }

    @http.route(['/cloud/import_host_instance'], type='jsonrpc', auth='user')
    def cloud_import_host_instance(self, host_id, path):
        """Import a running doodba instance from a host directory.

        Reads all config files via SSH, creates project + instance.
        """

        self._sec()._check_can_create_instance()
        host = request.env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}

        # Check not already imported
        existing = request.env['cloud.instance'].search([
            ('host_id', '=', host_id),
            ('custom_remote_dir', '=', path),
        ], limit=1)
        if existing:
            return {
                'ok': False,
                'error': _('This instance is already imported.'),
            }

        # Read all config files in one SSH connection
        files_to_read = [
            f'{path}/.copier-answers.yml',
            f'{path}/odoo/custom/src/repos.yaml',
            f'{path}/odoo/custom/src/addons.yaml',
            f'{path}/odoo/custom/dependencies/pip.txt',
            f'{path}/odoo/custom/dependencies/apt.txt',
            f'{path}/.docker/odoo.env',
            f'{path}/.docker/db-access.env',
            f'{path}/.docker/smtp.env',
            f'{path}/.docker/backup.env',
            f'{path}/docker-compose.override.yml',
        ]
        # Read all files + check symlink + docker compose ps
        commands = [
            f'cat "{f}" 2>/dev/null || echo ""' for f in files_to_read
        ] + [
            # conf.d: concatenate all .conf files
            f'cat {path}/odoo/custom/conf.d/*.conf 2>/dev/null || echo ""',
            # docker-compose.yml symlink target
            f'readlink -f "{path}/docker-compose.yml" 2>/dev/null || echo ""',
            # Container status
            f'cd "{path}" && docker compose ps --format '
            f"'{{{{.Service}}}}\\t{{{{.State}}}}' 2>/dev/null || echo ''",
        ]

        try:
            results = self._ssh_run_multi(host, commands)
        except Exception as exc:
            _logger.exception(
                "SSH import failed for host %s path %s", host_id, path,
            )
            return {'ok': False, 'error': str(exc)}

        # Unpack results
        (
            copier_raw, repos_raw, addons_raw,
            pip_raw, apt_raw,
            odoo_env_raw, db_env_raw, smtp_env_raw, backup_env_raw,
            override_raw,
        ) = [r[0] for r in results[:10]]
        conf_raw = results[10][0]
        compose_link = results[11][0].strip()
        compose_ps = results[12][0].strip()

        # Parse .copier-answers.yml
        copier = {}
        if copier_raw.strip():
            with suppress(Exception):
                data = yaml.safe_load(copier_raw) or {}
                domains = []
                for d in data.get('domains_prod', []) or []:
                    if isinstance(d, dict):
                        for h in d.get('hosts', []):
                            domains.append({
                                'hostname': h,
                                'redirect_to': d.get('redirect_to', ''),
                            })
                ov = data.get('odoo_version', '')
                copier = {
                    'odoo_version': f'{float(ov):.1f}' if ov else '',
                    'project_author': data.get('project_author', ''),
                    'project_license': data.get('project_license', ''),
                    'project_name': data.get('project_name', ''),
                    'postgres_version': str(
                        data.get('postgres_version', ''),
                    ),
                    'postgres_dbname': data.get('postgres_dbname', 'prod'),
                    'postgres_username': data.get(
                        'postgres_username', 'odoo',
                    ),
                    'odoo_proxy': data.get('odoo_proxy', 'traefik'),
                    'smtp_relay_host': data.get('smtp_relay_host', ''),
                    'smtp_relay_port': data.get('smtp_relay_port', 587),
                    'smtp_relay_user': data.get('smtp_relay_user', ''),
                    'domains': domains,
                    # Backup fields
                    'backup_dst': data.get('backup_dst', ''),
                    'backup_email_from': data.get(
                        'backup_email_from', '',
                    ),
                    'backup_email_to': data.get('backup_email_to', ''),
                    'backup_image_version': data.get(
                        'backup_image_version', 'latest',
                    ),
                    'backup_tz': data.get('backup_tz', 'UTC'),
                    'backup_deletion': data.get(
                        'backup_deletion', False,
                    ),
                    'backup_smtp_report_success': data.get(
                        'backup_smtp_report_success', True,
                    ),
                }

        # Parse .env files for secrets
        def _parse_env(content):
            env = {}
            for line in (content or '').splitlines():
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, _, v = line.partition('=')
                    env[k.strip()] = v.strip().strip('"').strip("'")
            return env

        odoo_env = _parse_env(odoo_env_raw)
        db_env = _parse_env(db_env_raw)
        smtp_env = _parse_env(smtp_env_raw)
        backup_env = _parse_env(backup_env_raw)

        # Parse repos.yaml + addons.yaml (reuse helpers from import_project)
        _SSH_RE = re.compile(r'^git@[^:]+:(.+?)(?:\.git)?$')

        def _ssh_to_https(u):
            u = (u or '').strip()
            m = _SSH_RE.match(u)
            if m:
                return f'https://github.com/{m.group(1)}.git'
            return u

        odoo_version = copier.get('odoo_version', '')
        repos_data = []
        if repos_raw.strip():
            try:
                repos_yaml = yaml.safe_load(repos_raw) or {}
                addons_yaml = yaml.safe_load(addons_raw) if addons_raw.strip() else {}
                addons_map = {}
                for alias, addons in (addons_yaml or {}).items():
                    if isinstance(addons, list):
                        addons_map[alias] = (
                            '' if '*' in addons
                            else ','.join(str(a) for a in addons)
                        )
                    elif addons == '*':
                        addons_map[alias] = ''
                    else:
                        addons_map[alias] = str(addons) if addons else ''

                for alias, cfg in repos_yaml.items():
                    if alias in ('./odoo', 'odoo'):
                        continue
                    if not isinstance(cfg, dict):
                        continue
                    remotes = cfg.get('remotes', {})
                    repo_url = next(iter(remotes.values()), '') if remotes else ''
                    repo_url = _ssh_to_https(repo_url)
                    target = cfg.get('target', '')
                    branch_val = ''
                    if target:
                        parts = target.split()
                        branch_val = parts[-1] if len(parts) >= 2 else parts[0]
                    if not branch_val:
                        merges = cfg.get('merges', [])
                        if merges and isinstance(merges[0], str):
                            parts = merges[0].split()
                            branch_val = parts[-1] if len(parts) >= 2 else parts[0]
                    if '$ODOO_VERSION' in branch_val and odoo_version:
                        branch_val = branch_val.replace(
                            '$ODOO_VERSION', odoo_version,
                        )
                    repos_data.append({
                        'url': repo_url,
                        'branch': branch_val or 'main',
                        'addons': addons_map.get(alias, ''),
                    })
            except Exception:
                _logger.exception("Failed to parse repos.yaml")

        # Parse docker-compose.override.yml for resource limits
        resource_limits = {}
        if override_raw.strip():
            with suppress(Exception):
                override = yaml.safe_load(override_raw) or {}
                services = override.get('services', {})
                for svc, key in (
                    ('odoo', 'odoo'), ('db', 'db'),
                    ('backup', 'backup'), ('smtp', 'smtp'),
                ):
                    svc_cfg = services.get(svc, {})
                    if svc_cfg.get('mem_limit'):
                        resource_limits[f'{key}_memory_limit'] = (
                            svc_cfg['mem_limit']
                        )
                    if svc_cfg.get('cpus'):
                        resource_limits[f'{key}_cpus'] = float(
                            svc_cfg['cpus'],
                        )

        # Determine environment from symlink
        environment = 'production'
        if compose_link and 'test.yaml' in compose_link:
            environment = 'staging'

        # Determine running status
        is_running = False
        if compose_ps:
            for line in compose_ps.splitlines():
                if 'odoo' in line.lower() and 'running' in line.lower():
                    is_running = True
                    break

        # ── Create project (or reuse existing) ────────────────────────
        project_name = copier.get('project_name') or path.rstrip('/').split('/')[-1]
        project = request.env['cloud.project'].search([
            ('name', '=', project_name),
        ], limit=1)
        if not project:
            proj_vals = {'name': project_name}
            if odoo_version:
                proj_vals['odoo_version'] = odoo_version
            if copier.get('project_author'):
                proj_vals['project_author'] = copier['project_author']
            if copier.get('project_license'):
                proj_vals['project_license'] = copier['project_license']
            if pip_raw.strip():
                proj_vals['pip_dependencies'] = pip_raw.strip()
            if apt_raw.strip():
                proj_vals['apt_dependencies'] = apt_raw.strip()
            project = request.env['cloud.project'].create(proj_vals)

            # Create project repos
            Repo = request.env['cloud.project.repo'].with_context(
                skip_apply_requirements=True,
            )
            for seq, r in enumerate(repos_data, start=10):
                if r.get('url'):
                    Repo.create({
                        'project_id': project.id,
                        'sequence': seq,
                        'url': r['url'],
                        'branch': r.get('branch', 'main'),
                        'addons': r.get('addons', ''),
                    })

        # ── Create instance ───────────────────────────────────────────
        inst_vals = {
            'name': path.rstrip('/').split('/')[-1],
            'project_id': project.id,
            'host_id': host_id,
            'environment': environment,
            'custom_remote_dir': path,
            'custom_backup_dst': copier.get('backup_dst', ''),
            'deployed': True,
            'running': is_running,
            'status': 'ok' if is_running else 'error',
        }
        if odoo_version:
            inst_vals['odoo_version'] = odoo_version
        if conf_raw.strip():
            inst_vals['odoo_conf'] = conf_raw.strip()
        if pip_raw.strip():
            inst_vals['pip_dependencies'] = pip_raw.strip()
        if apt_raw.strip():
            inst_vals['apt_dependencies'] = apt_raw.strip()

        # Copier-derived fields
        if copier:
            for k in (
                'postgres_version', 'postgres_dbname',
                'postgres_username', 'odoo_proxy',
                'smtp_relay_host', 'smtp_relay_user',
            ):
                if copier.get(k):
                    inst_vals[k] = copier[k]
            if copier.get('smtp_relay_port'):
                inst_vals['smtp_relay_port'] = int(copier['smtp_relay_port'])
            if copier.get('domains'):
                hostnames = [
                    (d.get('hostname') or '').strip()
                    for d in copier['domains']
                    if (d.get('hostname') or '').strip()
                ]
                if hostnames:
                    taken = set(
                        request.env['cloud.instance.domain']
                        .search([('hostname', 'in', hostnames)])
                        .mapped('hostname')
                    )
                    conflicts = taken & set(hostnames)
                    if conflicts:
                        return {
                            'ok': False,
                            'error': _(
                                'The following domains are already '
                                'assigned to another instance: %s. '
                                'Remove them from the existing instance '
                                'first, then retry the import.',
                                ', '.join(sorted(conflicts)),
                            ),
                        }
                    inst_vals['domain_ids'] = [
                        (0, 0, {
                            'hostname': d['hostname'].strip(),
                            'redirect_to': d.get('redirect_to', ''),
                        })
                        for d in copier['domains']
                        if (d.get('hostname') or '').strip()
                    ]

        # Secrets from .env files
        if odoo_env.get('ADMIN_PASSWORD'):
            inst_vals['odoo_admin_password'] = odoo_env['ADMIN_PASSWORD']
        if db_env.get('PASSWORD'):
            inst_vals['postgres_password'] = db_env['PASSWORD']
        if smtp_env.get('RELAY_PASSWORD'):
            inst_vals['smtp_relay_password'] = smtp_env['RELAY_PASSWORD']

        # Resource limits
        inst_vals.update(resource_limits)

        try:
            instance = request.env['cloud.instance'].create(inst_vals)
        except Exception as exc:
            _logger.exception("Failed to create instance during import")
            return {
                'ok': False,
                'error': str(exc),
            }

        # Handle backup backend from backup.env + copier backup fields
        if backup_env.get('AWS_ACCESS_KEY_ID'):
            try:
                # Parse backup_dst: "boto3+s3://bucket/path" → bucket, path
                s3_bucket = ''
                s3_path = ''
                backup_dst = copier.get('backup_dst', '')
                if '://' in backup_dst:
                    dst_path = backup_dst.split('://', 1)[1]
                    parts = dst_path.split('/', 1)
                    s3_bucket = parts[0]
                    s3_path = parts[1] if len(parts) > 1 else ''

                backend_name = (
                    f"S3 {copier.get('project_name') or 'import'}"
                )
                backend = request.env['cloud.backup.backend'].search([
                    ('name', '=', backend_name),
                ], limit=1)
                if not backend:
                    backend = request.env['cloud.backup.backend'].create({
                        'name': backend_name,
                        's3_bucket': s3_bucket,
                        's3_path': s3_path,
                        's3_access_key_id': backup_env.get(
                            'AWS_ACCESS_KEY_ID', '',
                        ),
                        's3_secret_access_key': backup_env.get(
                            'AWS_SECRET_ACCESS_KEY', '',
                        ),
                        's3_endpoint_url': backup_env.get(
                            'AWS_ENDPOINT_URL', '',
                        ),
                        'passphrase': backup_env.get('PASSPHRASE', ''),
                        'email_from': copier.get(
                            'backup_email_from', '',
                        ),
                        'email_to': copier.get('backup_email_to', ''),
                        'backup_image_version': copier.get(
                            'backup_image_version', 'latest',
                        ),
                        'backup_tz': copier.get('backup_tz', 'UTC'),
                        'deletion_via_cron': copier.get(
                            'backup_deletion', False,
                        ),
                        'smtp_report_success': copier.get(
                            'backup_smtp_report_success', True,
                        ),
                    })
                instance.backup_backend_id = backend.id
            except Exception:
                _logger.exception(
                    "Could not create backup backend for import",
                )

        return {
            'ok': True,
            'project_id': project.id,
            'instance_id': instance.id,
            'project_name': project.name,
            'instance_name': instance.name,
            'is_running': is_running,
        }

    # ── Container log streaming ────────────────────────────────────────────────

    @http.route(['/cloud/fetch_container_logs'], type='jsonrpc', auth='user')
    def cloud_fetch_container_logs(self, instance_id, service, lines=200):
        """SSH into the instance host and return recent Docker Compose logs."""

        if not request.env.user._is_internal():
            return {'ok': False, 'error': _('Unauthorized')}

        inst = request.env['cloud.instance'].sudo().browse(instance_id)
        if not inst.exists() or not inst.host_id:
            return {'ok': False, 'error': _('Instance not found')}

        host = inst.host_id
        inst_dir = inst.get_remote_dir()
        yaml_file = 'prod.yaml' if inst.environment == 'production' else 'test.yaml'
        tail = max(1, min(int(lines), 2000))
        # sanitize service name to prevent injection (alphanumeric, dash, underscore)
        if not re.match(r'^[a-zA-Z0-9_-]+$', service):
            return {'ok': False, 'error': _('Invalid service name')}

        command = (
            f"cd {inst_dir} && docker compose -f {yaml_file} "
            f"logs --no-color --tail={tail} {service} 2>&1"
        )

        async def _run():
            async with asyncssh.connect(**host.ssh_connect_kwargs()) as conn:
                result = await conn.run(command, check=False)
                return result.stdout or result.stderr or ''

        try:
            output = run_async(_run())
            return {'ok': True, 'lines': [l for l in output.splitlines() if l]}
        except Exception as exc:
            _logger.exception(
                "Error fetching container logs for instance %s", instance_id
            )
            return {'ok': False, 'error': str(exc)}

    # ── Backup backend endpoints ──────────────────────────────────────────────

    _BACKUP_BACKEND_ALLOWED = {
        'name', 'backend_type',
        's3_bucket', 's3_path', 's3_endpoint_url', 's3_access_key_id',
        's3_secret_access_key',
        'passphrase',
        'backup_image_version',
        'email_from', 'email_to', 'smtp_report_success',
        'backup_retention',
        'deletion_via_cron', 'backup_tz',
    }

    @http.route(['/cloud/get_backup_backends'], type='jsonrpc', auth='user')
    def cloud_get_backup_backends(self):
        backends = request.env['cloud.backup.backend'].search([])
        # Count instances per backend via direct + project assignment
        Instance = request.env['cloud.instance']
        all_instances = Instance.search([])
        usage = {}
        for inst in all_instances:
            bid = inst.effective_backup_backend.id
            if bid:
                usage[bid] = usage.get(bid, 0) + 1
        return [
            {
                'id': b.id,
                'name': b.name,
                'backend_type': b.backend_type,
                's3_bucket': b.s3_bucket or '',
                's3_path': b.s3_path or '',
                's3_endpoint_url': b.s3_endpoint_url or '',
                'backup_dst': b.backup_dst or '',
                'instance_count': usage.get(b.id, 0),
            }
            for b in backends
        ]

    @http.route(['/cloud/get_backup_backend'], type='jsonrpc', auth='user')
    def cloud_get_backup_backend(self, backend_id):
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found')}
        return {
            'id': b.id,
            'name': b.name,
            'backend_type': b.backend_type,
            's3_bucket': b.s3_bucket or '',
            's3_path': b.s3_path or '',
            's3_endpoint_url': b.s3_endpoint_url or '',
            's3_access_key_id': b.s3_access_key_id or '',
            'has_s3_secret_access_key': bool(b.s3_secret_access_key),
            'has_passphrase': bool(b.passphrase),
            'backup_image_version': b.backup_image_version or 'latest',
            'email_from': b.email_from or '',
            'email_to': b.email_to or '',
            'smtp_report_success': b.smtp_report_success,
            'backup_retention': b.backup_retention or '3M',
            'deletion_via_cron': b.deletion_via_cron,
            'backup_tz': b.backup_tz or 'UTC',
            'backup_dst': b.backup_dst or '',
        }

    @http.route(['/cloud/create_backup_backend'], type='jsonrpc', auth='user')
    def cloud_create_backup_backend(self, vals):
        self._sec()._check_can_manage_settings()
        safe = {
            k: v for k, v in vals.items()
            if k in self._BACKUP_BACKEND_ALLOWED
        }
        b = request.env['cloud.backup.backend'].create(safe)
        return {'id': b.id, 'name': b.name}

    @http.route(['/cloud/save_backup_backend'], type='jsonrpc', auth='user')
    def cloud_save_backup_backend(self, backend_id, vals):
        self._sec()._check_can_manage_settings()
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found')}
        safe = {
            k: v for k, v in vals.items()
            if k in self._BACKUP_BACKEND_ALLOWED
        }
        b.write(safe)
        return {'ok': True}

    @http.route(['/cloud/delete_backup_backend'], type='jsonrpc', auth='user')
    def cloud_delete_backup_backend(self, backend_id):
        self._sec()._check_can_manage_settings()
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found')}
        instances = request.env['cloud.instance'].search([
            ('backup_backend_id', '=', backend_id),
        ])
        if instances:
            names = ', '.join(instances.mapped('name'))
            return {
                'ok': False,
                'error': _(
                    'Cannot delete: this backend is used by %d '
                    'instance(s): %s. Remove the backend assignment '
                    'from those instances first.',
                    len(instances), names,
                ),
            }
        b.unlink()
        return {'ok': True}

    @http.route(['/cloud/test_backup_backend'], type='jsonrpc', auth='user')
    def cloud_test_backup_backend(self, backend_id):
        b = request.env['cloud.backup.backend'].browse(backend_id)
        if not b.exists():
            return {'ok': False, 'error': _('Backend not found.')}
        if not b.s3_bucket:
            return {'ok': False, 'error': _('No bucket configured.')}

        kwargs = {
            'aws_access_key_id':     b.s3_access_key_id or None,
            'aws_secret_access_key': b.s3_secret_access_key or None,
        }
        if b.s3_endpoint_url:
            kwargs['endpoint_url'] = b.s3_endpoint_url

        try:
            client = boto3.client('s3', **kwargs)
            client.head_bucket(Bucket=b.s3_bucket)
            return {'ok': True}
        except ParamValidationError as e:
            return {'ok': False, 'error': _('Invalid bucket name "%s": '
                    'bucket names cannot contain slashes. '
                    'Put only the bucket name here and the prefix in the Path field.') % b.s3_bucket}
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            msg = e.response.get('Error', {}).get('Message', str(e))
            if code in ('301', 'PermanentRedirect'):
                # Bucket exists but in a different region — still reachable
                return {'ok': True}
            if code == '403':
                return {
                    'ok': False,
                    'error': _('Access denied (403): check your credentials and bucket policy.'),
                }
            if code == '404':
                return {'ok': False, 'error': _('Bucket "%s" not found (404).') % b.s3_bucket}
            return {'ok': False, 'error': _('S3 error %(code)s: %(msg)s') % {'code': code, 'msg': msg}}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
