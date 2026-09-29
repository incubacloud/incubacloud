"""Host-shell endpoints — thin proxies in front of per-session subprocesses.

Same architecture as ``terminal`` (one subprocess per session plus a
routing table in the database), with its own models and endpoints: the
shell opens on the whole machine rather than inside a compose service.

It grants nothing the caller does not already hold. Opening one requires
the role that manages hosts, and that role already runs root on the host
through every host job (setup, hardening, teardown), with the same key
stored on the same ``cloud.host``. What the shell adds is interactivity,
and it adds it with more controls than a job has:

  * server-side guard ``_check_can_manage_hosts`` before any side effect;
  * the ``cloud.host`` is browsed WITHOUT sudo, so record rules apply;
  * at most one open session per (user, host);
  * its own rate limit, stricter than the instance terminal's;
  * a per-session Bearer token, encrypted at rest, checked with
    ``hmac.compare_digest`` on the loopback hop to the subprocess;
  * an audit row in ``cloud.host.session`` with client IP and user agent.
"""
import base64
import json
import logging
import os
import secrets
import uuid

from odoo import _, fields, http
from odoo.http import request

from ._client_ip import client_ip
from ._rate_limit import Rule, rate_gate_json
from .terminal_proxy_mixin import TerminalProxyMixin, spawn_subprocess

_logger = logging.getLogger(__name__)

_SID = '<string:session_id>'

# Absolute path to the per-session subprocess script and the addon dir it
# needs on ``sys.path`` for the shared bases (the same dir here).
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SUBPROCESS_PATH = os.path.join(_ADDON_DIR, 'host_terminal_subprocess.py')

# ANSI palette — kept here (not in host_terminal_session.py) so every
# visible string flows through ``_()``. The session module cannot call
# ``_()`` (it does not import odoo), so the banner is built here and
# handed over pre-rendered.
_ANSI_R     = '\x1b[0m'
_ANSI_B     = '\x1b[1m'
_ANSI_DIM   = '\x1b[2m'
_ANSI_WARN  = '\x1b[38;5;214m'   # orange
_ANSI_DANGER = '\x1b[38;5;196m'  # red
_ANSI_ACC   = '\x1b[38;5;45m'    # cyan
_ANSI_GRN   = '\x1b[38;5;82m'    # green (command names)
_ANSI_GREY  = '\x1b[38;5;245m'   # grey (descriptions)


class HostTerminalController(TerminalProxyMixin, http.Controller):

    _ROUTE_MODEL = 'cloud.host.terminal.route'

    # ── Open ───────────────────────────────────────────────────────────────

    @http.route(
        '/cloud/host_terminal/open',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def host_terminal_open(self, host_id):
        """Open a login shell on *host_id* for the current user.

        Returns ``{ok, session_id, host_name}`` on success, or
        ``{ok: False, error}`` when a limit or a precondition refuses it.
        """
        env = request.env
        # The same gate that shows the host action bar in the SPA.
        env['cloud.security.mixin']._check_can_manage_hosts()

        uid = env.user.id
        limited = rate_gate_json(
            Rule(
                f'host_console_user:{uid}',
                _(
                    'Too many host console sessions opened recently. '
                    'Try again in a minute.'
                ),
                cap_key='rate_limit_host_console_per_min',
                log_tag=f'host_console user={uid}',
            ),
            Rule(
                f'host_console:{host_id}',
                _(
                    'Too many host console sessions on this host. '
                    'Try again in a minute.'
                ),
                cap_key='rate_limit_host_console_per_min',
                log_tag=f'host_console host={host_id}',
            ),
        )
        if limited:
            return limited

        # Browsed WITHOUT sudo — record rules on cloud.host apply.
        host = env['cloud.host'].browse(host_id)
        if not host.exists():
            return {'ok': False, 'error': _('Host not found')}

        # One open session per (user, host): keeps zombie tabs from piling
        # up and bounds what one account can hold open.
        if env['cloud.host.session'].search_count([
            ('user_id', '=', uid),
            ('host_id', '=', host_id),
            ('state', '=', 'open'),
        ]):
            return {
                'ok': False,
                'error': _(
                    'You already have an open console on this host. '
                    'Close it first.'
                ),
            }

        sid = uuid.uuid4().hex
        auth_token = secrets.token_urlsafe(32)

        # Audit row first: if the spawn fails we still have a trace of who
        # tried to open what. It is closed by the client's /close or, when
        # abandoned, by the route GC's reconciliation.
        env['cloud.host.session'].create({
            'host_id': host_id,
            'session_id': sid,
            'user_id': uid,
            'client_ip': client_ip(),
            'user_agent': (request.httprequest.user_agent.string or '')[:255],
        })

        # ``ssh_connect_kwargs()`` returns an ``SSHKnownHosts`` object and
        # (for key auth) raw key bytes — neither JSON-serialisable. Strip
        # them and pass JSON-safe side channels; the subprocess re-injects
        # them before connecting.
        ssh_kwargs = dict(host.ssh_connect_kwargs())
        ssh_kwargs.pop('known_hosts', None)
        client_keys_b64 = [
            base64.b64encode(k).decode('ascii')
            for k in (ssh_kwargs.pop('client_keys', None) or [])
            if isinstance(k, (bytes, bytearray))
        ]
        port, pid = spawn_subprocess(
            session_id=sid,
            auth_token=auth_token,
            config={
                'ssh_connect_kwargs': ssh_kwargs,
                'client_keys_b64': client_keys_b64,
                'known_hosts_text': host.known_hosts_key or '',
                'host_label': host.name or host.ip_address,
                'user_id': uid,
                'welcome_banner': _render_host_welcome_banner(host),
            },
            subprocess_path=_SUBPROCESS_PATH,
            core_dir=_ADDON_DIR,
            tmp_prefix=f'ic-host-term-{sid[:8]}-',
            fail_label='host terminal subprocess',
        )

        env['cloud.host.terminal.route'].sudo().create({
            'session_id': sid,
            'pid': pid,
            'port': port,
            'auth_token': auth_token,
            'user_id': uid,
        })

        return {
            'ok': True,
            'session_id': sid,
            'host_name': host.name or host.ip_address,
        }

    # ── Page ───────────────────────────────────────────────────────────────

    @http.route(
        f'/cloud/host_terminal/{_SID}',
        type='http', auth='user',
    )
    def host_terminal_page(self, session_id, **kw):
        """Render the xterm.js page of a host shell the caller opened."""
        env = request.env
        sess_rec = env['cloud.host.session'].search(
            [('session_id', '=', session_id),
             ('user_id', '=', env.user.id)],
            limit=1,
        )
        if not sess_rec:
            return request.not_found()

        session_info = env['ir.http'].session_info()
        host = sess_rec.host_id
        return request.render(
            'incubacloud.host_terminal_page',
            {
                'session_id': session_id,
                'host_label': host.name or host.ip_address,
                'host_ip': host.ip_address or '',
                'host_port': host.port or 22,
                'host_user': host.user or '',
                'csrf_token': request.csrf_token(),
                'session_info': session_info,
                'json': json,
            },
        )

    # ── Output / Input / Resize / Close ────────────────────────────────────

    @http.route(
        f'/cloud/host_terminal/{_SID}/output',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def host_terminal_output(self, session_id, after_seq=0):
        """Return buffered output chunks with seq > after_seq."""
        return self._proxy(
            session_id, 'GET', '/output',
            params={'after': int(after_seq or 0)},
        )

    @http.route(
        f'/cloud/host_terminal/{_SID}/input',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def host_terminal_input(self, session_id, data):
        """Send base64-encoded bytes to the remote PTY."""
        return self._proxy(
            session_id, 'POST', '/input', body={'data': data},
        )

    @http.route(
        f'/cloud/host_terminal/{_SID}/resize',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def host_terminal_resize(self, session_id, cols=80, rows=24):
        """Send a PTY resize event."""
        return self._proxy(
            session_id, 'POST', '/resize',
            body={'cols': int(cols), 'rows': int(rows)},
        )

    @http.route(
        f'/cloud/host_terminal/{_SID}/close',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def host_terminal_close(self, session_id):
        """Close the shell, seal its audit row and drop its route."""
        env = request.env
        result = self._proxy(session_id, 'POST', '/close')
        rec = env['cloud.host.session'].search(
            [('session_id', '=', session_id),
             ('user_id', '=', env.user.id)],
            limit=1,
        )
        if rec and rec.state == 'open':
            rec.write({
                'state': 'closed',
                'closed_at': fields.Datetime.now(),
                'close_reason': (result or {}).get('close_reason') or 'user',
            })
        env['cloud.host.terminal.route'].sudo().search(
            [('session_id', '=', session_id)]
        ).unlink()
        return result


# ── Welcome banner ────────────────────────────────────────────────────────

def _render_host_welcome_banner(host):
    """Return the ANSI banner shown when a host shell opens.

    Alarming by design: warning colours (red/orange) instead of the
    friendly blue of the instance banner, so the operator sees at once
    that they are on the whole operating system, not inside a
    ``docker exec``.
    """
    title = _('HOST SHELL')
    content = f'   ⚠   IncubaCloud  —  {title}   '
    inner_width = max(45, len(content) + 2)
    pad = ' ' * (inner_width - len(content))
    bar = '═' * inner_width
    blank = ' ' * inner_width

    header = (
        f"\r\n"
        f"  {_ANSI_DANGER}{_ANSI_B}╔{bar}╗{_ANSI_R}\r\n"
        f"  {_ANSI_DANGER}{_ANSI_B}║{blank}║{_ANSI_R}\r\n"
        f"  {_ANSI_DANGER}{_ANSI_B}║{_ANSI_R}   "
        f"{_ANSI_WARN}⚠{_ANSI_R}   "
        f"{_ANSI_B}IncubaCloud{_ANSI_R}  —  "
        f"{_ANSI_DANGER}{_ANSI_B}{title}{_ANSI_R}   "
        f"{pad}"
        f"{_ANSI_DANGER}{_ANSI_B}║{_ANSI_R}\r\n"
        f"  {_ANSI_DANGER}{_ANSI_B}║{blank}║{_ANSI_R}\r\n"
        f"  {_ANSI_DANGER}{_ANSI_B}╚{bar}╝{_ANSI_R}\r\n"
        f"\r\n"
        f"  {_ANSI_GREY}{_('Host:')}{_ANSI_R}  "
        f"{_ANSI_B}{host.name or '—'}{_ANSI_R}\r\n"
        f"  {_ANSI_GREY}{_('IP:')}{_ANSI_R}    "
        f"{_ANSI_ACC}{host.ip_address or '—'}:{host.port or 22}{_ANSI_R}\r\n"
        f"  {_ANSI_GREY}{_('User:')}{_ANSI_R}  "
        f"{_ANSI_ACC}{host.user or '—'}{_ANSI_R}\r\n"
        f"  {_ANSI_DIM}{'─' * 38}{_ANSI_R}\r\n"
        f"  {_ANSI_DANGER}{_ANSI_B}"
        f"⚠  {_('Actions affect the entire server.')}{_ANSI_R}\r\n"
        f"  {_ANSI_WARN}"
        f"{_('Idle sessions auto-close after 2 minutes.')}{_ANSI_R}\r\n"
        f"  {_ANSI_WARN}"
        f"{_('Type `exit` when done.')}{_ANSI_R}\r\n"
        f"  {_ANSI_DIM}{'─' * 38}{_ANSI_R}\r\n"
    )

    cmds = [
        ('docker ps', _('List running containers')),
        ('docker compose ls', _('List compose projects')),
        ('df -h', _('Disk usage')),
        ('free -h', _('Memory usage')),
        ('uptime', _('Load and uptime')),
        ('journalctl -u docker --since "5 min ago"',
         _('Recent docker daemon logs')),
        ('systemctl status', _('System unit overview')),
    ]

    parts = [
        f"\r\n  {_ANSI_B}{_ANSI_ACC}"
        f"{_('Useful (read-only) commands:')}{_ANSI_R}\r\n",
    ]
    for cmd, desc in cmds:
        parts.append(
            f"  {_ANSI_GRN}{cmd}{_ANSI_R}\r\n"
            f"    {_ANSI_DIM}{desc}{_ANSI_R}\r\n"
        )
    parts.append('\r\n')
    return header + ''.join(parts)
