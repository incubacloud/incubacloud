"""
In-process SSH terminal sessions for the web terminal feature.

Each session:
  - Runs in a dedicated daemon thread with its own asyncio event loop.
  - Opens an SSH connection to the host, then runs a PTY shell inside the
    target Docker container (``docker exec -it <container> bash``).
  - Buffers output as raw bytes chunks (monotonic sequence numbers).
  - Accepts input bytes through a thread-safe list.
  - Auto-closes after SESSION_TIMEOUT seconds of inactivity.

Global registry: ``_sessions`` dict keyed by session_id.
"""

import asyncio
import logging
import threading
import time
import uuid
from contextlib import suppress

import asyncssh

_logger = logging.getLogger(__name__)

# Global session registry keyed by session_id → TerminalSession
_sessions: dict = {}
_sessions_lock = threading.Lock()

# Seconds of inactivity before a session is force-closed
SESSION_TIMEOUT = 120
# How often the cleanup daemon scans for expired sessions
_CLEANUP_INTERVAL = 30


def get_session(session_id):
    with _sessions_lock:
        return _sessions.get(session_id)


def close_and_remove_session(session_id):
    with _sessions_lock:
        sess = _sessions.pop(session_id, None)
    if sess:
        sess.close()


def register_session(sess):
    with _sessions_lock:
        _sessions[sess.session_id] = sess
    _ensure_cleanup_daemon()


# ── Background cleanup daemon ───────────────────────────────────────────────

_cleanup_started = False
_cleanup_lock = threading.Lock()


def _ensure_cleanup_daemon():
    global _cleanup_started
    with _cleanup_lock:
        if _cleanup_started:
            return
        _cleanup_started = True
    t = threading.Thread(target=_cleanup_loop, daemon=True, name='terminal-cleanup')
    t.start()


def _cleanup_loop():
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        try:
            _evict_expired()
        except Exception:
            _logger.exception("terminal cleanup error")


def _evict_expired():
    with _sessions_lock:
        expired = [(sid, s) for sid, s in _sessions.items() if s.is_expired()]
        for sid, _ in expired:
            _sessions.pop(sid)
    for sid, sess in expired:
        _logger.info("terminal session %s timed out", sid[:8])
        sess.close(reason='timeout')


class TerminalSession:
    """Interactive SSH terminal session.

    Lifecycle:
      1. ``__init__`` spawns a daemon thread.
      2. The thread connects via SSH, locates the Docker container, opens a PTY.
      3. Callers poll ``read_output(after_seq)`` and push data via ``write_input``.
      4. ``close()`` gracefully shuts down the SSH process and thread.
    """

    def __init__(
        self,
        session_id,
        ssh_connect_kwargs,
        inst_dir,
        service,
        instance_name='',
        environment='',
    ):
        self.session_id = session_id
        self._ssh_connect_kwargs = ssh_connect_kwargs
        self.host = ssh_connect_kwargs.get('host', '')
        self.port = ssh_connect_kwargs.get('port', 22)
        self.inst_dir = inst_dir
        self.service = service
        self.instance_name = instance_name
        self.environment = environment

        # Output buffer: list of (seq, bytes)
        self._output_buffer: list = []
        self._output_lock = threading.Lock()
        self._output_seq = 0

        # Input queue: list of bytes (None = close signal)
        self._input_queue: list = []
        self._input_lock = threading.Lock()

        self._loop = None
        self._process = None
        self._closed = False
        self._connected = False
        self._error = None
        self._close_reason = None  # 'timeout' | None (user/eof)
        # Inactivity is tracked only by user input, not server output,
        # so a long-running command doesn't reset the idle clock.
        self._last_input = time.time()

        self._thread = threading.Thread(target=self._run, daemon=True, name=f"terminal-{session_id[:8]}")
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def connected(self):
        return self._connected

    @property
    def closed(self):
        return self._closed

    @property
    def error(self):
        return self._error

    @property
    def close_reason(self):
        return self._close_reason

    def idle_seconds(self):
        """Seconds since the last keystroke from the user."""
        return time.time() - self._last_input

    def is_expired(self):
        return self.idle_seconds() > SESSION_TIMEOUT

    def read_output(self, after_seq=0):
        """Return ``[(seq, bytes), ...]`` with seq > after_seq."""
        with self._output_lock:
            return [(s, d) for s, d in self._output_buffer if s > after_seq]

    def write_input(self, data: bytes):
        self._last_input = time.time()  # reset idle clock on every keystroke
        with self._input_lock:
            self._input_queue.append(data)

    def resize(self, cols: int, rows: int):
        self._last_input = time.time()
        if self._process and self._loop and not self._closed:
            asyncio.run_coroutine_threadsafe(
                self._process.change_terminal_size(cols, rows),
                self._loop,
            )

    def close(self, reason=None):
        self._close_reason = reason
        self._closed = True
        with self._input_lock:
            self._input_queue.append(None)  # wake the writer coroutine

    # ── Welcome banner ─────────────────────────────────────────────────────

    def _build_welcome_banner(self) -> str:
        """Return an ANSI-coloured welcome message as a string."""
        R  = '\x1b[0m'       # reset
        B  = '\x1b[1m'       # bold
        DIM = '\x1b[2m'      # dim
        # Colours
        BRAND  = '\x1b[38;5;33m'   # IncubaCloud blue
        ACCENT = '\x1b[38;5;45m'   # cyan
        WARN   = '\x1b[38;5;214m'  # orange (production warning)
        GRN    = '\x1b[38;5;82m'   # green (command names)
        GREY   = '\x1b[38;5;245m'  # grey (descriptions)

        env_colours = {
            'production': '\x1b[38;5;196m',  # red
            'staging':    '\x1b[38;5;214m',  # orange
            'runbot':     '\x1b[38;5;82m',   # green
        }
        env_label = (self.environment or 'unknown').capitalize()
        env_col   = env_colours.get(self.environment, ACCENT)

        header = (
            f"\r\n"
            f"  {BRAND}{B}☁  IncubaCloud Terminal{R}\r\n"
            f"  {DIM}{'─' * 38}{R}\r\n"
            f"  {GREY}Instance:{R}  {B}{self.instance_name or '—'}{R}\r\n"
            f"  {GREY}Env:{R}       {env_col}{B}{env_label}{R}\r\n"
            f"  {GREY}Service:{R}   {ACCENT}{self.service or 'odoo'}{R}\r\n"
            f"  {DIM}{'─' * 38}{R}\r\n"
        )

        if self.environment == 'production':
            header += (
                f"  {WARN}{B}⚠  You are on PRODUCTION. Be careful.{R}\r\n"
                f"  {DIM}{'─' * 38}{R}\r\n"
            )

        # Service-aware command hints
        if self.service == 'db':
            list_dbs = (
                "SELECT datname FROM pg_database"
                " WHERE datistemplate=false ORDER BY datname;"
            )
            cmds = [
                ('psql -U odoo -d prod',                  'PostgreSQL console'),
                (f'psql -U odoo -d prod -c "{list_dbs}"', 'List all databases'),
                ('psql -U odoo -d <dbname>',              'Connect to a specific database'),
            ]
        elif self.service == 'odoo':
            cmds = [
                ('odoo shell --no-http',                           'Interactive Python/Odoo console'),
                ('odoo -u <module> --stop-after-init --no-http',   'Update a module'),
                ('psql',                                           'PostgreSQL console'),
                ('ls /opt/odoo/auto/addons/',                      'List available addons'),
                ('cat /opt/odoo/auto/odoo.conf',                   'View generated Odoo config'),
                ('env | grep ODOO',                                'Show ODOO_* env vars'),
                ('pip list | grep odoo',                           'List installed Odoo packages'),
            ]
        else:
            cmds = []

        if cmds:
            lines = [f"\r\n  {B}{ACCENT}Useful commands:{R}\r\n"]
            for cmd, desc in cmds:
                lines.append(f"  {GRN}{cmd}{R}\r\n    {DIM}{desc}{R}\r\n")
            lines.append('\r\n')
        else:
            lines = ['\r\n']

        return header + ''.join(lines)

    # ── Private ────────────────────────────────────────────────────────────

    def _append_output(self, data: bytes):
        with self._output_lock:
            self._output_seq += 1
            self._output_buffer.append((self._output_seq, data))
            # Cap the buffer to avoid unbounded growth
            if len(self._output_buffer) > 10_000:
                self._output_buffer = self._output_buffer[-5_000:]

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_run())
        except Exception as exc:
            self._error = str(exc)
            _logger.exception("TerminalSession %s failed", self.session_id)
        finally:
            self._closed = True
            with suppress(Exception):
                self._loop.close()

    async def _async_run(self):
        connect_kw = dict(self._ssh_connect_kwargs)
        connect_kw['keepalive_interval'] = 30

        async with asyncssh.connect(**connect_kw) as conn:
            # Resolve the container ID for the requested service
            result = await conn.run(
                f"cd {self.inst_dir} && docker compose ps -q {self.service} 2>/dev/null | head -1",
                check=False,
            )
            container_id = (result.stdout or '').strip()

            if container_id:
                cmd = (
                    f"docker exec -it {container_id} bash 2>/dev/null"
                    f" || docker exec -it {container_id} sh"
                )
            else:
                cmd = (
                    f"cd {self.inst_dir}"
                    f" && docker compose exec -T {self.service} bash 2>/dev/null"
                    f" || cd {self.inst_dir} && docker compose exec -T {self.service} sh"
                )

            async with conn.create_process(
                cmd,
                request_pty=True,
                term_type='xterm-256color',
                term_size=(80, 24),
                encoding=None,  # raw bytes — xterm.js handles encoding
            ) as process:
                self._process = process
                self._connected = True
                self._append_output(self._build_welcome_banner().encode())

                async def _read_output():
                    # Use read() not `async for` — the iterator waits for \n
                    # but PTY prompts have no trailing newline, so they'd block.
                    while True:
                        chunk = await process.stdout.read(65536)
                        if not chunk:
                            break
                        self._append_output(chunk)

                async def _write_input():
                    while not self._closed:
                        await asyncio.sleep(0.02)
                        with self._input_lock:
                            chunks = self._input_queue.copy()
                            self._input_queue.clear()
                        for data in chunks:
                            if data is None:
                                process.stdin.close()
                                return
                            process.stdin.write(data)

                await asyncio.gather(_read_output(), _write_input())
