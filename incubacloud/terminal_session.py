"""
``TerminalSession`` — SSH + PTY session lifecycle, reusable from
inside ``incubacloud.terminal_subprocess``.

Previously this module also kept an in-memory ``_sessions`` dict to
let HTTP controllers look up active sessions. That registry
broke under multi-worker Odoo: only the worker that opened the
session could find it in its own memory. The fix (P1.25) moves
session ownership into a per-session subprocess and uses
``cloud.terminal.route`` as the shared lookup store, so this
module now only defines the class itself.

The class is deliberately free of ``odoo.*`` imports so the
subprocess can import it without paying the cost of the Odoo
registry bootstrap on every new terminal (~10 s per open).
"""

import asyncio
import logging
import shlex
import threading
import time
import uuid
from contextlib import suppress

import asyncssh

_logger = logging.getLogger(__name__)

# Seconds of inactivity (no user keystrokes) before a session is
# force-closed by the subprocess watchdog.
SESSION_TIMEOUT = 120


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
        user_id=None,
        welcome_banner='',
    ):
        self.session_id = session_id
        self._ssh_connect_kwargs = ssh_connect_kwargs
        self.host = ssh_connect_kwargs.get('host', '')
        self.port = ssh_connect_kwargs.get('port', 22)
        self.inst_dir = inst_dir
        self.service = service
        self.instance_name = instance_name
        self.environment = environment
        self.user_id = user_id
        # Pre-rendered, ANSI-coloured, already-translated welcome text.
        # The controller builds it (it has ``_()`` available); this
        # subprocess-side class stays free of ``odoo.*`` imports.
        self.welcome_banner = welcome_banner or ''

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
            # Resolve the container ID for the requested service.
            # Quote inst_dir preserving the ~/ prefix (shlex.quote would
            # prevent tilde expansion by wrapping the whole string in quotes).
            svc = shlex.quote(self.service)
            _d = self.inst_dir
            safe_dir = (
                '~/' + shlex.quote(_d[2:]) if _d.startswith('~/')
                else shlex.quote(_d)
            )
            result = await conn.run(
                f"cd {safe_dir} && docker compose ps -q {svc} 2>/dev/null | head -1",
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
                    f"cd {safe_dir}"
                    f" && docker compose exec -T {svc} bash 2>/dev/null"
                    f" || cd {safe_dir} && docker compose exec -T {svc} sh"
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
                if self.welcome_banner:
                    self._append_output(self.welcome_banner.encode())

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
