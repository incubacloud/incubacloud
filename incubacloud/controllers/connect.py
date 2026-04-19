import json
import logging
import re
import shlex

import asyncssh

from odoo import http, _
from odoo.http import request

from .async_utils import run_async

_CONTAINER_ID_RE = re.compile(r'^[a-f0-9]{12,64}$')

_logger = logging.getLogger(__name__)

# ── Python scripts executed inside the Odoo container via docker exec ─────────

_GET_USERS_SCRIPT = """\
import json
try:
    import psycopg2
    conn = psycopg2.connect(host='db', dbname={db!r}, user={user!r}, password={password!r})
    cur = conn.cursor()
    cur.execute(
        "SELECT u.id, p.name, u.login FROM res_users u "
        "JOIN res_partner p ON p.id = u.partner_id "
        "WHERE u.active = true AND u.share = false ORDER BY p.name"
    )
    print(json.dumps({{'ok': True, 'users': [
        {{'id': r[0], 'name': r[1], 'login': r[2]}} for r in cur.fetchall()
    ]}}))
    conn.close()
except Exception as e:
    print(json.dumps({{'ok': False, 'error': str(e)}}))
"""

_INJECT_SESSION_SCRIPT = """\
import json, time, os, secrets
try:
    import psycopg2
    conn = psycopg2.connect(host='db', dbname={db!r}, user={user!r}, password={password!r})
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM res_users WHERE id = %s AND active = true",
        ({uid},)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        print(json.dumps({{'ok': False, 'error': 'User not found or inactive'}}))
    else:
        token_dir = '/tmp/ic_tokens'
        os.makedirs(token_dir, mode=0o700, exist_ok=True)
        os.chmod(token_dir, 0o700)
        ic_token = secrets.token_hex(16)
        token_path = os.path.join(token_dir, ic_token)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as tf:
            json.dump({{'uid': {uid}, 'db': {db!r}, 'ts': time.time()}}, tf)
        print(json.dumps({{'ok': True, 'token': ic_token}}))
except Exception as e:
    print(json.dumps({{'ok': False, 'error': str(e)}}))
"""


# ── SSH helpers ────────────────────────────────────────────────────────────────


async def _get_container_id(conn, inst_dir, yaml_file):
    """Return the container ID of the odoo service, or empty string."""
    # ~ must stay outside quotes for shell expansion; only quote the subpath.
    if inst_dir.startswith('~/'):
        safe_dir = '~/' + shlex.quote(inst_dir[2:])
    else:
        safe_dir = shlex.quote(inst_dir)
    result = await conn.run(
        f"cd {safe_dir} && docker compose -f {shlex.quote(yaml_file)} ps -q odoo"
        " 2>/dev/null | head -1",
        check=False,
    )
    cid = (result.stdout or '').strip()
    # Validate: Docker container IDs are hex strings (short 12-char or full 64-char)
    if not _CONTAINER_ID_RE.match(cid):
        return ''
    return cid


async def _run_in_container(conn, container_id, script):
    """Run a Python script inside the container via docker exec -i."""
    result = await conn.run(
        f"docker exec -i {container_id} python3 -",
        input=script,
        check=False,
    )
    return (result.stdout or '').strip()


# ── Controller ─────────────────────────────────────────────────────────────────

class InstanceConnectController(http.Controller):

    def _inst_dir(self, inst):
        return inst.get_remote_dir()

    def _yaml_file(self, inst):
        return 'prod.yaml' if inst.environment == 'production' else 'test.yaml'

    # ── Get users ─────────────────────────────────────────────────────────────

    @http.route(['/cloud/get_instance_users'], type='jsonrpc', auth='user')
    def get_instance_users(self, instance_id):
        if not request.env.user._is_internal():
            return {'ok': False, 'error': _('Unauthorized')}

        inst = request.env['cloud.instance'].sudo().browse(instance_id)
        if not inst.exists() or not inst.host_id:
            return {'ok': False, 'error': _('Instance not found or has no host')}
        if not inst.deployed or not inst.running:
            return {'ok': False, 'error': _('Instance is not running')}

        host = inst.host_id
        inst_dir = self._inst_dir(inst)
        yaml_file = self._yaml_file(inst)

        script = _GET_USERS_SCRIPT.format(
            db=inst.postgres_dbname or 'prod',
            user=inst.postgres_username or 'odoo',
            password=inst.postgres_password or '',
        )

        async def _run():
            async with asyncssh.connect(**host.ssh_connect_kwargs()) as conn:
                container_id = await _get_container_id(
                    conn, inst_dir, yaml_file
                )
                if not container_id:
                    return {
                        'ok': False,
                        'error': _('Odoo container not found or not running'),
                    }
                output = await _run_in_container(conn, container_id, script)
                if not output:
                    return {'ok': False, 'error': _('No output from container')}
                return json.loads(output)

        try:
            return run_async(_run())
        except Exception:
            _logger.exception(
                "Error getting instance users for %s", instance_id
            )
            return {'ok': False, 'error': _('An internal error occurred. Check server logs.')}

    # ── Prepare session ────────────────────────────────────────────────────────

    @http.route(['/cloud/prepare_instance_connect'], type='jsonrpc', auth='user')
    def prepare_instance_connect(self, instance_id, user_id, user_name=None):
        """Inject a pre-authenticated session into the instance and return
        a one-time URL pointing to the instance's /ic/login endpoint.

        The incubacloud_connect module must be installed on the instance.
        It provides /ic/login which reads the token file written here and
        sets the session cookie from the instance's own domain, avoiding
        cross-domain cookie restrictions entirely.
        """
        if not request.env.user._is_internal():
            return {'ok': False, 'error': _('Unauthorized')}

        if not isinstance(user_id, int) or user_id <= 0:
            return {'ok': False, 'error': _('Invalid user_id')}

        inst = request.env['cloud.instance'].sudo().browse(instance_id)
        if not inst.exists() or not inst.host_id:
            return {'ok': False, 'error': _('Instance not found')}
        if not inst.deployed or not inst.running:
            return {'ok': False, 'error': _('Instance is not running')}
        if not inst.domain:
            return {'ok': False, 'error': _('Instance has no domain configured')}

        host = inst.host_id
        inst_dir = self._inst_dir(inst)
        yaml_file = self._yaml_file(inst)

        script = _INJECT_SESSION_SCRIPT.format(
            db=inst.postgres_dbname or 'prod',
            user=inst.postgres_username or 'odoo',
            password=inst.postgres_password or '',
            uid=user_id,
        )

        async def _run():
            async with asyncssh.connect(**host.ssh_connect_kwargs()) as conn:
                container_id = await _get_container_id(
                    conn, inst_dir, yaml_file
                )
                if not container_id:
                    return {'ok': False, 'error': _('Odoo container not found')}
                output = await _run_in_container(conn, container_id, script)
                if not output:
                    return {'ok': False, 'error': _('No output from container')}
                return json.loads(output)

        try:
            result = run_async(_run())
        except Exception:
            _logger.exception(
                "Error injecting session for instance %s", instance_id
            )
            return {'ok': False, 'error': _('An internal error occurred. Check server logs.')}

        if not result.get('ok'):
            return result

        domain = inst.domain.strip()
        ic_token = result.get('token', '')
        if not ic_token:
            return {'ok': False, 'error': _('No token returned from instance')}

        # Audit trail: record the connect-as action
        label = user_name or str(user_id)
        request.env['cloud.audit.log'].sudo().create({
            'action': 'Connect as user',
            'instance_id': inst.id,
            'details': label,
        })

        return {
            'ok': True,
            'url': f'https://{domain}/ic/login?t={ic_token}',
        }

    @http.route('/cloud/get_audit_log', type='jsonrpc', auth='user', methods=['POST'])
    def get_audit_log(
        self, instance_id=None, host_id=None, limit=100,
        q=None, action_filter=None, date_from=None, date_to=None,
    ):
        return request.env['cloud.job'].get_audit_log(
            instance_id=instance_id,
            host_id=host_id,
            limit=limit,
            q=q,
            action_filter=action_filter,
            date_from=date_from,
            date_to=date_to,
        )

    @http.route('/cloud/purge_audit_logs', type='jsonrpc', auth='user', methods=['POST'])
    def purge_audit_logs(self, days=None):
        if not request.env.user.has_group('incubacloud.group_cloud_manager'):
            return {'ok': False, 'error': 'Unauthorized'}
        if days is None:
            ICP = request.env['ir.config_parameter'].sudo()
            days = int(ICP.get_param('incubacloud.audit_log_retention_days', '90') or 90)
        count = request.env['cloud.audit.log']._purge_old(days)
        return {'ok': True, 'deleted': count}

    @http.route('/cloud/get_user_preferences', type='jsonrpc', auth='user',
                methods=['POST'])
    def get_user_preferences(self):
        user = request.env.user
        return {
            'ok': True,
            'cloud_notification_level': user.cloud_notification_level or 'failures',
        }

    @http.route('/cloud/save_user_preferences', type='jsonrpc', auth='user',
                methods=['POST'])
    def save_user_preferences(self, cloud_notification_level=None):
        valid = {'all', 'failures', 'none'}
        if cloud_notification_level not in valid:
            return {'ok': False, 'error': 'Invalid notification level'}
        request.env.user.sudo().write({
            'cloud_notification_level': cloud_notification_level,
        })
        return {'ok': True}
