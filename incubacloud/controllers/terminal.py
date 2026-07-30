"""Terminal endpoints — thin proxies in front of per-session subprocesses.

Why the indirection:
    Each terminal's SSH socket + PTY is a live OS resource that cannot
    move between processes. When Odoo runs with ``--workers > 1``,
    storing the session in a worker's in-memory dict means only that
    one worker can serve subsequent polls — every other worker
    returns 404 and the UI shows "closed".

Architecture:
    * ``/cloud/terminal/open`` spawns a subprocess
      (``incubacloud.terminal_subprocess``) that owns the SSH socket
      and binds a random free port on ``127.0.0.1``.
    * A row in ``cloud.terminal.route`` (Postgres) maps the session id
      to the subprocess PID + port + auth token. Any worker can read
      this, so any worker can serve subsequent requests.
    * ``/cloud/terminal/<sid>/input|output|resize|close`` look up the
      route row and forward the HTTP request to the subprocess. The
      subprocess is exclusively reachable from ``127.0.0.1`` so the
      Bearer token kept in the route row is the only credential
      between Odoo and the subprocess.

Trade-off:
    One Python process per live session. A ``TerminalSession`` is
    cheap (~20 MB) and the typical workload has <20 concurrent
    sessions, so the footprint is negligible. In exchange we get
    multi-worker correctness without sticky cookies or a central
    daemon.
"""
import base64
import json
import os
import re
import secrets
import uuid

from odoo import fields, http, _
from odoo.http import request

from ._rate_limit import Rule, rate_gate_json
from .terminal_proxy_mixin import TerminalProxyMixin, spawn_subprocess

# Valid docker-compose service name: alphanumeric start, then [a-zA-Z0-9_.-]
_VALID_SERVICE_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,62}$')

_SID = '<string:session_id>'

# Absolute path to the per-session subprocess script and the core addon
# dir it needs on ``sys.path`` (this IS the core addon, so they coincide).
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SUBPROCESS_PATH = os.path.join(_ADDON_DIR, 'terminal_subprocess.py')


class TerminalController(TerminalProxyMixin, http.Controller):

    _ROUTE_MODEL = 'cloud.terminal.route'

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

        # Rate-limit (P1.22) — per-user and per-instance, unchanged
        # from before the subprocess refactor.
        uid = request.env.user.id
        limited = rate_gate_json(
            Rule(
                f'terminal_user:{uid}',
                _(
                    'Too many terminal sessions opened recently. '
                    'Try again in a minute.'
                ),
                cap_key='rate_limit_terminal_user_per_min',
                log_tag=f'terminal user={uid}',
            ),
            Rule(
                f'terminal_instance:{instance_id}',
                _(
                    'Too many terminal sessions open against this '
                    'instance. Try again in a minute.'
                ),
                cap_key='rate_limit_terminal_per_min',
                log_tag=f'terminal instance={instance_id}',
            ),
        )
        if limited:
            return limited

        env = request.env
        inst = env['cloud.instance'].browse(instance_id)
        if not inst.exists():
            return {'ok': False, 'error': _('Instance not found')}
        host = inst.host_id
        if not host:
            return {'ok': False, 'error': _('Instance has no host')}
        if not inst.deployed or not inst.running:
            return {'ok': False, 'error': _('Instance is not running')}

        sid = uuid.uuid4().hex
        auth_token = secrets.token_urlsafe(32)

        # Persist the user-facing audit row first so that even if
        # the subprocess spawn fails we still have a trace.
        env['cloud.instance.session'].create({
            'instance_id': instance_id,
            'service': service,
            'session_id': sid,
            'user_id': env.user.id,
        })

        # Spawn the subprocess and record its port in the route
        # table. If anything fails we let the error bubble (wrapped
        # by safe_error_response at the outer frame via the generic
        # try/except that the SPA shows as a toast).
        #
        # ``ssh_connect_kwargs()`` returns a dict that includes an
        # asyncssh ``SSHKnownHosts`` object and (for key-auth hosts) a
        # list of raw key bytes — neither is JSON-serialisable. Strip
        # both and pass JSON-safe side channels; the subprocess
        # re-injects them before calling ``asyncssh.connect``.
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
                'inst_dir': inst.get_remote_dir(),
                'service': service,
                'instance_name': inst.name,
                'environment': inst.environment or '',
                'user_id': env.user.id,
                'welcome_banner': _render_welcome_banner(inst, service),
            },
            subprocess_path=_SUBPROCESS_PATH,
            core_dir=_ADDON_DIR,
            tmp_prefix=f'ic-terminal-{sid[:8]}-',
            fail_label='terminal subprocess',
        )

        env['cloud.terminal.route'].sudo().create({
            'session_id': sid,
            'pid': pid,
            'port': port,
            'auth_token': auth_token,
            'user_id': env.user.id,
        })

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

    # ── Output polling ─────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/output',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_output(self, session_id, after_seq=0):
        """Return buffered output chunks with seq > after_seq."""
        result = self._proxy(
            session_id, 'GET', '/output',
            params={'after': int(after_seq or 0)},
        )
        # The subprocess returns ``chunks`` as ``[{seq, data}, ...]``;
        # the UI expects the same shape so we pass through untouched.
        return result

    # ── Input ──────────────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/input',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_input(self, session_id, data):
        """Send base64-encoded bytes to the remote PTY."""
        return self._proxy(
            session_id, 'POST', '/input', body={'data': data},
        )

    # ── Resize ─────────────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/resize',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_resize(self, session_id, cols=80, rows=24):
        """Send a PTY resize event."""
        return self._proxy(
            session_id, 'POST', '/resize',
            body={'cols': int(cols), 'rows': int(rows)},
        )

    # ── Close ──────────────────────────────────────────────────────────────

    @http.route(
        f'/cloud/terminal/{_SID}/close',
        type='jsonrpc', auth='user', methods=['POST'],
    )
    def terminal_close(self, session_id):
        """Close the terminal session and update the audit record."""
        env = request.env
        # Proxy the close — the subprocess exits after responding
        # and the PID becomes reapable.
        result = self._proxy(session_id, 'POST', '/close')
        rec = env['cloud.instance.session'].search(
            [('session_id', '=', session_id),
             ('user_id', '=', env.user.id)],
            limit=1,
        )
        if rec and rec.state == 'open':
            rec.write({'state': 'closed', 'closed_at': fields.Datetime.now()})
        # Aggressive cleanup: drop the routing row (and its bearer
        # token) right now instead of waiting for the cleanup cron
        # or the next _resolve() call. Reduces the window during
        # which the encrypted auth_token sits in the DB.
        env['cloud.terminal.route'].sudo().search(
            [('session_id', '=', session_id)]
        ).unlink()
        return result


# ── Welcome banner ─────────────────────────────────────────────────────────

# ANSI palette — kept here (not in terminal_session.py) so every visible
# string can flow through ``_()`` for translation. The subprocess-side
# module deliberately avoids ``odoo.*`` imports, so it can't call ``_()``
# itself; building the banner here and shipping a pre-rendered string is
# the trade-off.
_ANSI_R     = '\x1b[0m'
_ANSI_B     = '\x1b[1m'
_ANSI_DIM   = '\x1b[2m'
_ANSI_BRAND = '\x1b[38;5;33m'    # IncubaCloud blue
_ANSI_ACC   = '\x1b[38;5;45m'    # cyan
_ANSI_WARN  = '\x1b[38;5;214m'   # orange
_ANSI_GRN   = '\x1b[38;5;82m'    # green (command names)
_ANSI_GREY  = '\x1b[38;5;245m'   # grey (descriptions)

_ENV_COLOURS = {
    'production': '\x1b[38;5;196m',  # red
    'staging':    '\x1b[38;5;214m',  # orange
}


def _render_welcome_banner(instance, service):
    """Return the ANSI-coloured welcome text shown at terminal open.

    Doodba-specific. The listed commands surface tools Tecnativa ships
    with the image (``addons``) and click-odoo-contrib helpers
    (``click-odoo``, ``click-odoo-update``) that operators rarely know
    about otherwise. All visible strings go through ``_()`` so a
    translated ``.po`` kicks in per user language.
    """
    env_label = (instance.environment or _("Unknown")).capitalize()
    env_col = _ENV_COLOURS.get(instance.environment, _ANSI_ACC)

    # Box banner — chunky, coloured, branded. The interior width is
    # computed from the actual (translated) title so the right border
    # always lines up. We avoid real figlet/ASCII art so the result
    # stays readable in any font and respects 80-col terminals.
    title = _('Remote Terminal')
    content = f'   ☁   IncubaCloud  —  {title}   '
    inner_width = max(45, len(content) + 2)
    pad = ' ' * (inner_width - len(content))
    bar = '═' * inner_width
    blank = ' ' * inner_width

    header = (
        f"\r\n"
        f"  {_ANSI_BRAND}{_ANSI_B}╔{bar}╗{_ANSI_R}\r\n"
        f"  {_ANSI_BRAND}{_ANSI_B}║{blank}║{_ANSI_R}\r\n"
        f"  {_ANSI_BRAND}{_ANSI_B}║{_ANSI_R}   ☁   "
        f"{_ANSI_B}IncubaCloud{_ANSI_R}  —  "
        f"{_ANSI_ACC}{title}{_ANSI_R}   "
        f"{pad}"
        f"{_ANSI_BRAND}{_ANSI_B}║{_ANSI_R}\r\n"
        f"  {_ANSI_BRAND}{_ANSI_B}║{blank}║{_ANSI_R}\r\n"
        f"  {_ANSI_BRAND}{_ANSI_B}╚{bar}╝{_ANSI_R}\r\n"
        f"\r\n"
        f"  {_ANSI_GREY}{_('Instance:')}{_ANSI_R}  "
        f"{_ANSI_B}{instance.name or '—'}{_ANSI_R}\r\n"
        f"  {_ANSI_GREY}{_('Env:')}{_ANSI_R}       "
        f"{env_col}{_ANSI_B}{env_label}{_ANSI_R}\r\n"
        f"  {_ANSI_GREY}{_('Service:')}{_ANSI_R}   "
        f"{_ANSI_ACC}{service or 'odoo'}{_ANSI_R}\r\n"
        f"  {_ANSI_DIM}{'─' * 38}{_ANSI_R}\r\n"
    )

    if instance.environment == 'production':
        header += (
            f"  {_ANSI_WARN}{_ANSI_B}"
            f"⚠  {_('You are on PRODUCTION. Be careful.')}{_ANSI_R}\r\n"
            f"  {_ANSI_DIM}{'─' * 38}{_ANSI_R}\r\n"
        )

    if service == 'db':
        list_dbs_sql = (
            'SELECT datname FROM pg_database'
            ' WHERE datistemplate=false ORDER BY datname;'
        )
        cmds = [
            ('psql -U odoo -d prod', _('PostgreSQL console')),
            (
                f'psql -U odoo -d prod -c "{list_dbs_sql}"',
                _('List all databases'),
            ),
            ('psql -U odoo -d <dbname>', _('Connect to a specific database')),
        ]
    elif service == 'odoo':
        cmds = [
            (
                'click-odoo-update',
                _('Auto-detect changed modules by checksum and update only those'),
            ),
            (
                'click-odoo',
                _('Python shell with --rollback by default (safe — nothing commits)'),
            ),
            (
                'odoo shell --no-http',
                _('Interactive Odoo/Python shell (commits by default — use with care)'),
            ),
            ('psql', _('PostgreSQL console')),
            (
                'cat /opt/odoo/auto/odoo.conf',
                _('View the generated Odoo config (addons_path, workers, limits)'),
            ),
            (
                'addons --help',
                _('doodba addons CLI — list, install, update and test addons'),
            ),
        ]
    elif service == 'backup':
        cmds = [
            (
                'duplicity collection-status "$DST"',
                _('List available backup chains on the remote backend'),
            ),
            (
                'duplicity list-current-files "$DST"',
                _('List files in the most recent backup'),
            ),
            (
                'cat /etc/backup.env',
                _('View configured destination and credentials'),
            ),
        ]
    else:
        cmds = []

    if cmds:
        parts = [
            f"\r\n  {_ANSI_B}{_ANSI_ACC}{_('Useful commands:')}{_ANSI_R}\r\n",
        ]
        for cmd, desc in cmds:
            parts.append(
                f"  {_ANSI_GRN}{cmd}{_ANSI_R}\r\n"
                f"    {_ANSI_DIM}{desc}{_ANSI_R}\r\n"
            )
        parts.append('\r\n')
    else:
        parts = ['\r\n']

    return header + ''.join(parts)
