import json
import logging
import re
import shlex
import urllib.request

import asyncssh

from odoo import http, _
from odoo.exceptions import AccessError
from odoo.http import request

from ..net.outbound import OutboundError, validate_url

from ._rate_limit import Rule, rate_gate_json
from .async_utils import run_async

_CONTAINER_ID_RE = re.compile(r'^[a-f0-9]{12,64}$')

_logger = logging.getLogger(__name__)

# ── Python scripts executed inside the Odoo container via docker exec ─────────

# Flags a target user as an administrator of the tenant database: uid 2
# (Odoo's built-in admin) or explicit membership of base.group_system /
# base.group_erp_manager. The relation table and the ir_model_data rows
# are stable across Odoo 7 → 19, so the same SQL works on every tenant
# version we support. Over-flagging is harmless: it only raises the
# required panel role to Manager.
# It is kept on a single line because it is interpolated inside a
# double-quoted string literal of the generated script.
_IS_ADMIN_SQL = (
    "(u.id = 2 OR EXISTS (SELECT 1 FROM res_groups_users_rel r "
    "JOIN ir_model_data d ON d.model = 'res.groups' AND d.res_id = r.gid "
    "WHERE r.uid = u.id AND d.module = 'base' "
    "AND d.name IN ('group_system', 'group_erp_manager')))"
)

_GET_USERS_SCRIPT = """\
import json
try:
    import psycopg2
    conn = psycopg2.connect(host='db', dbname={db!r}, user={user!r}, password={password!r})
    cur = conn.cursor()
    cur.execute(
        "SELECT u.id, p.name, u.login, {is_admin_sql} FROM res_users u "
        "JOIN res_partner p ON p.id = u.partner_id "
        "WHERE u.active = true AND u.share = false ORDER BY p.name"
    )
    print(json.dumps({{'ok': True, 'users': [
        {{'id': r[0], 'name': r[1], 'login': r[2], 'is_admin': bool(r[3])}}
        for r in cur.fetchall()
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
        "SELECT u.id, {is_admin_sql} FROM res_users u "
        "WHERE u.id = %s AND u.active = true",
        ({uid},)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        print(json.dumps({{'ok': False, 'error': 'User not found or inactive'}}))
    elif row[1] and not {allow_admin!r}:
        print(json.dumps({{'ok': False, 'is_admin': True, 'error':
            'Connecting as an administrator of a production instance '
            'requires the Manager role.'}}))
    else:
        token_dir = '/tmp/ic_tokens'
        os.makedirs(token_dir, mode=0o700, exist_ok=True)
        os.chmod(token_dir, 0o700)
        ic_token = secrets.token_hex(16)
        token_path = os.path.join(token_dir, ic_token)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as tf:
            json.dump({{'uid': {uid}, 'db': {db!r}, 'ts': time.time(),
                        'by': {by!r}}}, tf)
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

    # ── Rate limiting ─────────────────────────────────────────────────────────
    # Shared gate (decision log P13): the hit/log/deny block lives in
    # ``_rate_limit``; this wrapper only declares connect's two windows.

    def _connect_rate_limited(self, instance_id):
        """Return an error dict when the caller is over a connect-as cap.

        Two tumbling windows: one per panel user (a compromised account
        minting tokens in bulk) and one per instance (several operators
        hammering the same tenant). Returns ``None`` when allowed.
        """
        uid = request.env.user.id
        return rate_gate_json(
            Rule(
                f'connect_user:{uid}',
                _(
                    'Too many connect-as requests recently. '
                    'Try again in a minute.'
                ),
                cap_key='rate_limit_connect_user_per_min',
                log_tag=f'connect user={uid}',
            ),
            Rule(
                f'connect_instance:{instance_id}',
                _(
                    'Too many connect-as requests against this instance. '
                    'Try again in a minute.'
                ),
                cap_key='rate_limit_connect_per_min',
                log_tag=f'connect instance={instance_id}',
            ),
        )

    def _resolve_instance(self, instance_id):
        """Browse *instance_id* as the caller and return it, or an error dict.

        Browsing without sudo first is deliberate: the record rules on
        ``cloud.instance`` scope visibility per project, and a user who
        cannot read the instance must not learn it exists.
        """
        inst = request.env['cloud.instance'].browse(instance_id)
        if not inst.exists():
            return None, {'ok': False, 'error': _('Instance not found')}
        try:
            inst.check_access('read')
        except AccessError:
            return None, {'ok': False, 'error': _('Instance not found')}
        inst = inst.sudo()
        if not inst.host_id:
            return None, {
                'ok': False,
                'error': _('Instance not found or has no host'),
            }
        if not inst.deployed or not inst.running:
            return None, {'ok': False, 'error': _('Instance is not running')}
        return inst, None

    # ── Get users ─────────────────────────────────────────────────────────────

    @http.route(['/cloud/get_instance_users'], type='jsonrpc', auth='user')
    def get_instance_users(self, instance_id):
        """List the tenant's internal users, flagging its administrators.

        The instance is resolved before the permission check because the
        required role depends on its environment: listing the users of a
        production instance is already a Developer-level action.
        """
        inst, err = self._resolve_instance(instance_id)
        if err:
            return err
        request.env['cloud.security.mixin']._check_can_connect_as_user(
            instance=inst,
        )
        limited = self._connect_rate_limited(instance_id)
        if limited:
            return limited

        host = inst.host_id
        inst_dir = self._inst_dir(inst)
        yaml_file = self._yaml_file(inst)

        script = _GET_USERS_SCRIPT.format(
            db=inst.postgres_dbname or 'prod',
            user=inst.postgres_username or 'odoo',
            password=inst.postgres_password or '',
            is_admin_sql=_IS_ADMIN_SQL,
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
            result = run_async(_run())
        except Exception:
            _logger.exception(
                "Error getting instance users for %s", instance_id
            )
            return {'ok': False, 'error': _('An internal error occurred. Check server logs.')}

        if result.get('ok'):
            request.env['cloud.audit.log'].sudo().create({
                'action': 'List instance users',
                'instance_id': inst.id,
            })
        return result

    # ── Prepare session ────────────────────────────────────────────────────────

    @http.route(['/cloud/prepare_instance_connect'], type='jsonrpc', auth='user')
    def prepare_instance_connect(self, instance_id, user_id, user_name=None):
        """Inject a pre-authenticated session into the instance and return
        a one-time URL pointing to the instance's /ic/login endpoint.

        The incubacloud_connect module must be installed on the instance.
        It provides /ic/login which reads the token file written here and
        sets the session cookie from the instance's own domain, avoiding
        cross-domain cookie restrictions entirely.

        Whether the target user is an administrator of the tenant is
        decided inside the tenant database, not by the caller: the panel
        passes ``allow_admin`` and the script refuses to mint a token for
        an admin when it is False. A client that lies about ``user_name``
        (a display label only) therefore cannot widen its own access.
        """
        sec = request.env['cloud.security.mixin']

        if not isinstance(user_id, int) or user_id <= 0:
            return {'ok': False, 'error': _('Invalid user_id')}

        inst, err = self._resolve_instance(instance_id)
        if err:
            return err
        # Floor check for this environment: connecting as a *normal* user.
        # The admin case is enforced below, once the tenant tells us
        # whether the target is one.
        sec._check_can_connect_as_user(instance=inst)
        limited = self._connect_rate_limited(instance_id)
        if limited:
            return limited
        if not inst.domain:
            return {'ok': False, 'error': _('Instance has no domain configured')}

        allow_admin = (
            inst.environment != 'production'
            or sec._has_cloud_group('group_cloud_manager')
        )

        host = inst.host_id
        inst_dir = self._inst_dir(inst)
        yaml_file = self._yaml_file(inst)

        script = _INJECT_SESSION_SCRIPT.format(
            db=inst.postgres_dbname or 'prod',
            user=inst.postgres_username or 'odoo',
            password=inst.postgres_password or '',
            uid=user_id,
            is_admin_sql=_IS_ADMIN_SQL,
            allow_admin=allow_admin,
            by=request.env.user.login,
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
            if result.get('is_admin'):
                _logger.warning(
                    "connect_as_admin_denied user=%s instance=%s target_uid=%s",
                    request.env.user.login, inst.id, user_id,
                )
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
            'details': f'{label} [{inst.environment}]',
        })

        return {
            'ok': True,
            'url': f'https://{domain}/ic/login?t={ic_token}',
        }

    @http.route('/cloud/get_audit_log', type='jsonrpc', auth='user', methods=['POST'])
    def get_audit_log(
        self, instance_id=None, host_id=None, limit=100, offset=0,
        q=None, action_filter=None, date_from=None, date_to=None,
    ):
        return request.env['cloud.job'].get_audit_log(
            instance_id=instance_id,
            host_id=host_id,
            limit=limit,
            offset=offset,
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
        # sudo for the muted names only: the user chose these projects
        # while they were visible; a later membership change must not
        # break the preferences modal.
        muted = [
            {'id': p.id, 'name': p.name}
            for p in user.cloud_muted_project_ids.sudo()
        ]
        return {
            'ok': True,
            'cloud_notification_level': user.cloud_notification_level or 'failures',
            'cloud_notification_mode': user.cloud_notification_mode or 'immediate',
            'cloud_email_enabled': user.cloud_email_enabled,
            'cloud_muted_projects': muted,
            'cloud_telegram_configured': bool(user.cloud_telegram_bot_token),
            'cloud_telegram_chat_id': user.cloud_telegram_chat_id or '',
            'cloud_webhook_configured': bool(user.cloud_webhook_secret),
            'cloud_webhook_url': user.cloud_webhook_url or '',
        }

    @http.route('/cloud/save_user_preferences', type='jsonrpc', auth='user',
                methods=['POST'])
    def save_user_preferences(self, cloud_notification_level=None,
                              cloud_notification_mode=None,
                              cloud_muted_project_ids=None,
                              cloud_email_enabled=None,
                              cloud_telegram_bot_token=None,
                              cloud_telegram_chat_id=None,
                              cloud_webhook_url=None,
                              cloud_webhook_secret=None):
        """Persist the caller's notification preferences.

        Muted ids are sanitised to existing projects; muting only
        restricts the caller's own notifications, so no visibility
        check is needed beyond existence.
        """
        if not request.env.user._is_internal():
            # The SPA that owns this form is internal-only (``/cloud``
            # returns 404 to a share user), so no legitimate caller is
            # turned away here. What this closes is the direct RPC: a
            # portal customer could store a webhook URL that only stays
            # inert because both senders filter ``share = False``
            # elsewhere. Correct today by accident in another file is
            # not a property worth keeping on an SSRF sink.
            return {'ok': False, 'error': 'Not available for this account'}
        valid = {'all', 'failures', 'none'}
        if cloud_notification_level not in valid:
            return {'ok': False, 'error': 'Invalid notification level'}
        if cloud_notification_mode not in ('immediate', 'daily_digest'):
            return {'ok': False, 'error': 'Invalid delivery mode'}
        vals = {
            'cloud_notification_level': cloud_notification_level,
            'cloud_notification_mode': cloud_notification_mode,
        }
        if cloud_email_enabled is not None:
            vals['cloud_email_enabled'] = bool(cloud_email_enabled)
        if cloud_muted_project_ids is not None:
            try:
                ids = [int(i) for i in cloud_muted_project_ids]
            except (TypeError, ValueError):
                return {'ok': False, 'error': 'Invalid muted project ids'}
            projects = request.env['cloud.project'].sudo().browse(ids).exists()
            vals['cloud_muted_project_ids'] = [(6, 0, projects.ids)]
        if cloud_telegram_bot_token:
            vals['cloud_telegram_bot_token'] = cloud_telegram_bot_token
        if cloud_telegram_chat_id is not None:
            vals['cloud_telegram_chat_id'] = cloud_telegram_chat_id.strip()
        if cloud_webhook_url is not None:
            url = cloud_webhook_url.strip()
            if url:
                # Checked here so a typo is reported while the user is
                # looking at the form. It is not the security boundary:
                # DNS can change between now and the notification, so
                # ``post_json`` validates again and pins the address it
                # approved. See :mod:`..net.outbound`.
                try:
                    validate_url(url)
                except OutboundError as exc:
                    return {'ok': False, 'error': f'Webhook URL rejected: {exc}'}
            vals['cloud_webhook_url'] = url
        if cloud_webhook_secret:
            vals['cloud_webhook_secret'] = cloud_webhook_secret
        request.env.user.sudo().write(vals)
        return {'ok': True}

    @http.route('/cloud/telegram_detect_chat_id', type='jsonrpc', auth='user',
                methods=['POST'])
    def telegram_detect_chat_id(self):
        """Poll the user's Telegram bot for the most recent chat_id.

        Calls ``getUpdates`` and returns the chat_id of the last
        message the bot received. The user must send at least one
        message to their bot first.
        """
        token = request.env.user.cloud_telegram_bot_token
        if not token:
            return {'ok': False, 'error': 'No Telegram bot token configured'}
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — only Telegram API (HTTPS)
                data = json.loads(resp.read())
        except Exception as exc:
            _logger.warning('telegram getUpdates failed: %s', exc)
            return {'ok': False, 'error': 'Failed to contact Telegram API'}
        if not data.get('ok'):
            return {'ok': False, 'error': data.get('description', 'Unknown error')}
        updates = data.get('result', [])
        if not updates:
            return {'ok': False, 'error': 'No messages received yet. Send any message to your bot first.'}
        last = updates[-1]
        chat = (last.get('message', {})
                .get('chat', {}))
        chat_id = chat.get('id') or (last.get('channel_post', {})
                                     .get('chat', {})
                                     .get('id'))
        if not chat_id:
            return {'ok': False, 'error': 'Could not determine chat ID from latest update'}
        return {'ok': True, 'chat_id': str(chat_id)}

    @http.route('/cloud/telegram_send_test', type='jsonrpc', auth='user',
                methods=['POST'])
    def telegram_send_test(self):
        """Send a test message to the user's configured Telegram chat."""
        token = request.env.user.cloud_telegram_bot_token
        chat_id = request.env.user.cloud_telegram_chat_id
        if not token:
            return {'ok': False, 'error': 'No Telegram bot token configured'}
        if not chat_id:
            return {'ok': False, 'error': 'No Telegram chat ID configured'}
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            'chat_id': chat_id,
            'text': '\U0001F7E2 IncubaCloud test notification — your Telegram integration is working.',
            'parse_mode': 'Markdown',
        }).encode()
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — only Telegram API (HTTPS)
                data = json.loads(resp.read())
        except Exception as exc:
            _logger.warning('telegram sendTest failed: %s', exc)
            return {'ok': False, 'error': 'Failed to contact Telegram API'}
        if not data.get('ok'):
            return {'ok': False, 'error': data.get('description', 'Unknown error')}
        return {'ok': True}
