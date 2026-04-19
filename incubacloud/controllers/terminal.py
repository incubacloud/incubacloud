import base64
import json
import logging
import re
import uuid

from odoo import fields, http, _
from odoo.http import request

from ..models.terminal_session import (
    SESSION_TIMEOUT,
    TerminalSession,
    close_and_remove_session,
    get_session,
    register_session,
)

_logger = logging.getLogger(__name__)

# Valid docker-compose service name: alphanumeric start, then [a-zA-Z0-9_.-]
_VALID_SERVICE_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,62}$')

_SID = '<string:session_id>'


class TerminalController(http.Controller):

    # ── Open session ───────────────────────────────────────────────────────

    @http.route(
        '/cloud/terminal/open', type='jsonrpc', auth='user', methods=['POST']
    )
    def terminal_open(self, instance_id, service='odoo'):
        """Create a terminal session for the given instance service.

        Returns ``{ok, session_id, instance_name, service}`` on success.
        """
        request.env['cloud.security.mixin']._check_can_use_terminal()
        if not _VALID_SERVICE_RE.match(service):
            return {'ok': False, 'error': _('Invalid service name')}
        env = request.env
        inst = env['cloud.instance'].browse(instance_id)
        if not inst.exists():
            return {'ok': False, 'error': _('Instance not found')}

        host = inst.host_id
        if not host:
            return {'ok': False, 'error': _('Instance has no host')}

        if not inst.deployed or not inst.running:
            return {'ok': False, 'error': _('Instance is not running')}

        inst_dir = inst.get_remote_dir()

        sid = uuid.uuid4().hex

        # Audit record
        env['cloud.instance.session'].create({
            'instance_id': instance_id,
            'service': service,
            'session_id': sid,
            'user_id': env.user.id,
        })

        term = TerminalSession(
            session_id=sid,
            ssh_connect_kwargs=host.ssh_connect_kwargs(),
            inst_dir=inst_dir,
            service=service,
            instance_name=inst.name,
            environment=inst.environment or '',
            user_id=env.user.id,
        )
        register_session(term)

        return {
            'ok': True,
            'session_id': sid,
            'instance_name': inst.name,
            'service': service,
        }

    # ── Terminal page ──────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}', type='http', auth='user'
    )
    def terminal_page(self, session_id, **kw):
        """Render the interactive xterm.js page."""
        env = request.env
        sess_rec = env['cloud.instance.session'].search(
            [('session_id', '=', session_id),
             ('user_id', '=', env.user.id)],
            limit=1,
        )
        if not sess_rec:
            return request.not_found()

        session_info = request.env['ir.http'].session_info()
        return request.render('incubacloud.cloud_terminal_page', {
            'session_id': session_id,
            'instance_name': sess_rec.instance_id.name,
            'service': sess_rec.service or 'odoo',
            'csrf_token': request.csrf_token(),
            'session_info': session_info,
            'json': json,
        })

    # ── Ownership helper ───────────────────────────────────────────────────

    @staticmethod
    def _owned_session(session_id):
        """Return the session if it belongs to the current user, else None."""
        sess = get_session(session_id)
        if sess and sess.user_id != request.env.user.id:
            return None
        return sess

    # ── Output polling ─────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/output',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_output(self, session_id, after_seq=0):
        """Return buffered output chunks with seq > after_seq.

        Also evicts the session if it has exceeded the idle timeout.
        """
        sess = self._owned_session(session_id)
        if not sess:
            return {
                'ok': True, 'chunks': [], 'connected': False,
                'closed': True, 'close_reason': 'timeout', 'error': None,
                'idle_seconds': SESSION_TIMEOUT,
            }

        if sess.is_expired():
            close_and_remove_session(session_id)
            return {
                'ok': True, 'chunks': [], 'connected': False,
                'closed': True, 'close_reason': 'timeout', 'error': None,
                'idle_seconds': int(sess.idle_seconds()),
            }

        chunks = sess.read_output(after_seq)
        return {
            'ok': True,
            'chunks': [
                {'seq': seq, 'data': base64.b64encode(data).decode()}
                for seq, data in chunks
            ],
            'connected': sess.connected,
            'closed': sess.closed,
            'close_reason': sess.close_reason,
            'error': sess.error,
            'idle_seconds': int(sess.idle_seconds()),
        }

    # ── Input ──────────────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/input',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_input(self, session_id, data):
        """Send base64-encoded bytes to the remote PTY."""
        sess = self._owned_session(session_id)
        if not sess or sess.closed:
            return {'ok': False}
        sess.write_input(base64.b64decode(data))
        return {'ok': True}

    # ── Resize ─────────────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/resize',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_resize(self, session_id, cols=80, rows=24):
        """Send a PTY resize event."""
        sess = self._owned_session(session_id)
        if not sess or sess.closed:
            return {'ok': False}
        sess.resize(int(cols), int(rows))
        return {'ok': True}

    # ── Close ──────────────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/close',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_close(self, session_id):
        """Close the terminal session and update the audit record."""
        sess = self._owned_session(session_id)
        if not sess:
            return {'ok': False, 'error': _('Session not found')}
        close_and_remove_session(session_id)
        env = request.env
        rec = env['cloud.instance.session'].search(
            [('session_id', '=', session_id),
             ('user_id', '=', env.user.id)],
            limit=1,
        )
        if rec and rec.state == 'open':
            rec.write({'state': 'closed', 'closed_at': fields.Datetime.now()})
        return {'ok': True}
