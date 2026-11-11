"""Shared base for interactive SSH + PTY terminal sessions.

Home of the buffer / lock / lifecycle machinery common to:

  * the instance terminal — ``incubacloud.terminal_session.TerminalSession``,
    scoped to a ``docker exec`` inside a compose service; and
  * the host console — ``incubacloud.host_terminal_session
    .HostTerminalSession``, a login shell on the whole box.

Deliberately free of ``odoo.*`` imports so the per-session subprocess can
import it without paying the ~10 s Odoo registry bootstrap on every new
terminal.

Security boundary — why this base carries NO ``create_process`` call:
    Opening a *command-less* PTY yields a host login shell. That
    capability must exist ONLY in the host subclass, reached through an
    endpoint reserved to whoever manages hosts — never as a fallback of
    the container terminal, which developers use. The base connects the
    SSH transport and then delegates process creation to the abstract
    ``_open_process`` hook, which each subclass implements with its own
    ``conn.create_process(...)`` call. A subclass that forgot to override
    it raises ``NotImplementedError`` — it never silently falls back to a
    host shell.
"""

import asyncio
import logging
import threading
import time
from contextlib import suppress

import asyncssh

_logger = logging.getLogger(__name__)

# Seconds of inactivity (no user keystrokes) before a session is
# force-closed by the subprocess watchdog. Single source of truth —
# both concrete session modules and both controllers read it from here.
SESSION_TIMEOUT = 120

# Longest a read of the output may wait for something new, in seconds,
# whatever the caller asks. The wait holds the Odoo worker that proxies
# it, so it stays short: long enough for the echo of a keystroke from a
# host in the same region, not a long poll.
MAX_OUTPUT_WAIT = 0.5


class BaseTerminalSession:
    """Interactive SSH terminal session — transport, buffers, lifecycle.

    Lifecycle:
      1. ``__init__`` stores connection params and prepares the buffers
         but does NOT start the worker thread.
      2. The concrete subclass sets its own attributes and then calls
         ``start()`` — deferring the thread start avoids a race where the
         worker reads a subclass attribute before ``__init__`` set it.
      3. The thread connects via SSH, calls the subclass ``_open_process``
         hook to obtain the PTY process, then pumps I/O.
      4. Callers poll ``read_output(after_seq)`` and push data via
         ``write_input``. ``close()`` gracefully shuts the process down.
    """

    # Prefix for the worker thread name; overridden per subclass so the
    # instance terminal and host console are distinguishable in tracebacks.
    _THREAD_PREFIX = 'terminal'

    def __init__(
        self,
        session_id,
        ssh_connect_kwargs,
        user_id=None,
        welcome_banner='',
    ):
        """Store the connection params and initialise the I/O buffers.

        The worker thread is NOT started here — the subclass must call
        ``start()`` after setting its own attributes.
        """
        self.session_id = session_id
        self._ssh_connect_kwargs = ssh_connect_kwargs
        self.host = ssh_connect_kwargs.get('host', '')
        self.port = ssh_connect_kwargs.get('port', 22)
        self.user_id = user_id
        # Pre-rendered, ANSI-coloured, already-translated welcome text.
        # The controller builds it (it has ``_()`` available); this
        # subprocess-side class stays free of ``odoo.*`` imports.
        self.welcome_banner = welcome_banner or ''

        # Output buffer: list of (seq, bytes)
        self._output_buffer: list = []
        self._output_lock = threading.Lock()
        # Wakes a read waiting for output (``read_output(wait=...)``).
        self._output_cond = threading.Condition(self._output_lock)
        self._output_seq = 0

        # Input queue: list of bytes (None = close signal)
        self._input_queue: list = []
        self._input_lock = threading.Lock()
        # The last batch of input applied, as ``(stream, seq)``: the page
        # sends a batch again when it did not get an answer, and one that
        # already arrived must not reach the shell twice.
        self._input_stream = None
        self._input_seq = 0

        self._loop = None
        self._process = None
        self._closed = False
        self._connected = False
        self._error = None
        self._close_reason = None  # 'timeout' | None (user/eof)
        # Inactivity is tracked only by user input, not server output,
        # so a long-running command doesn't reset the idle clock.
        self._last_input = time.time()

        self._thread = None

    def start(self):
        """Spawn the daemon worker thread.

        Called by the concrete subclass at the end of its ``__init__``,
        once every attribute the ``_open_process`` hook relies on is set.
        """
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"{self._THREAD_PREFIX}-{self.session_id[:8]}",
        )
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def connected(self):
        """True once the PTY process is open."""
        return self._connected

    @property
    def closed(self):
        """True once the session has been torn down."""
        return self._closed

    @property
    def error(self):
        """The stringified fatal error, if the worker died, else None."""
        return self._error

    @property
    def close_reason(self):
        """Why the session closed ('timeout' or None for user/eof)."""
        return self._close_reason

    def idle_seconds(self):
        """Seconds since the last keystroke from the user."""
        return time.time() - self._last_input

    def is_expired(self):
        """True when the session has been idle past ``SESSION_TIMEOUT``."""
        return self.idle_seconds() > SESSION_TIMEOUT

    def read_output(self, after_seq=0, wait=0.0):
        """Return ``[(seq, bytes), ...]`` with seq > after_seq.

        :param int after_seq: the last chunk the caller already has.
        :param float wait: seconds to wait for a chunk when there is none
            yet, capped at ``MAX_OUTPUT_WAIT``; it returns as soon as one
            arrives or the session closes, and ``0`` does not wait.
        :return: the chunks after ``after_seq``, possibly none.
        """
        deadline = time.monotonic() + min(max(wait or 0.0, 0.0), MAX_OUTPUT_WAIT)
        with self._output_cond:
            while True:
                chunks = [(s, d) for s, d in self._output_buffer if s > after_seq]
                remaining = deadline - time.monotonic()
                if chunks or self._closed or remaining <= 0:
                    return chunks
                # In slices, so a close (which does not notify) is seen.
                self._output_cond.wait(min(remaining, 0.05))

    def write_input(self, data: bytes, seq=None, stream=None):
        """Queue bytes for the remote PTY and reset the idle clock.

        :param bytes data: what was typed.
        :param int seq: the batch's number in the page's *stream*, or
            ``None`` for input that is never sent twice.
        :param str stream: the page that numbered the batch.
        :return: ``False`` when the batch was already applied (a resend
            whose first try arrived), ``True`` when it was queued.
        """
        self._last_input = time.time()  # reset idle clock on every keystroke
        with self._input_lock:
            if seq is not None:
                if stream == self._input_stream and seq <= self._input_seq:
                    return False
                self._input_stream, self._input_seq = stream, seq
            self._input_queue.append(data)
        return True

    def resize(self, cols: int, rows: int):
        """Resize the remote PTY window.

        ``change_terminal_size`` is a plain method of asyncssh's process,
        not a coroutine: it was handed to ``run_coroutine_threadsafe``,
        which raised, so no resize ever reached the shell (11-nov-2026).
        It runs on the session's loop, which owns the channel.
        """
        self._last_input = time.time()
        if self._process and self._loop and not self._closed:
            self._loop.call_soon_threadsafe(
                self._process.change_terminal_size, cols, rows,
            )

    def close(self, reason=None):
        """Signal the worker to shut down; ``reason`` records why."""
        self._close_reason = reason
        self._closed = True
        with self._input_lock:
            self._input_queue.append(None)  # wake the writer coroutine

    # ── Private ────────────────────────────────────────────────────────────

    def _append_output(self, data: bytes):
        """Append a chunk to the ring buffer, capping unbounded growth."""
        with self._output_cond:
            self._output_seq += 1
            self._output_buffer.append((self._output_seq, data))
            # Cap the buffer to avoid unbounded growth
            if len(self._output_buffer) > 10_000:
                self._output_buffer = self._output_buffer[-5_000:]
            self._output_cond.notify_all()

    def _run(self):
        """Thread entrypoint: own an event loop and drive ``_async_run``."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_run())
        except Exception as exc:
            self._error = str(exc)
            _logger.exception(
                "%s %s failed", type(self).__name__, self.session_id,
            )
        finally:
            self._closed = True
            with suppress(Exception):
                self._loop.close()

    async def _async_run(self):
        """Connect over SSH, open the PTY via the hook, then pump I/O."""
        connect_kw = dict(self._ssh_connect_kwargs)
        connect_kw['keepalive_interval'] = 30

        async with asyncssh.connect(**connect_kw) as conn:
            process = await self._open_process(conn)
            self._process = process
            self._connected = True
            if self.welcome_banner:
                self._append_output(self.welcome_banner.encode())
            await self._pump(process)

    async def _open_process(self, conn):
        """Open and return the asyncssh PTY process for this session.

        Abstract — each subclass builds its own ``conn.create_process``
        call. The base intentionally provides no default so that the
        host-shell capability (a command-less PTY) cannot leak into a
        subclass that merely forgot to override this hook.
        """
        raise NotImplementedError(
            "%s must implement _open_process" % type(self).__name__
        )

    async def _pump(self, process):
        """Shuttle bytes between the PTY and the in-memory buffers."""
        async def _read_output():
            # Use read() not `async for` — the iterator waits for \n
            # but PTY prompts have no trailing newline, so they'd block.
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                self._append_output(chunk)
            # The remote side ended (``exit``, the container stopped, the
            # link dropped), and so does the session. The writer below
            # stops only on ``_closed``: without this the session outlived
            # its shell, and the page said "Connected" until the idle
            # timeout (8-oct-2026).
            self._closed = True

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
