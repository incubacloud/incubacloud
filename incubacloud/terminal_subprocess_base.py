"""Shared machinery for the per-session terminal subprocesses.

Both the instance terminal (``incubacloud.terminal_subprocess``) and the
host console (``incubacloud_saas_manager.host_terminal_subprocess``) run
one subprocess per live session that owns the SSH socket and serves a
tiny loopback HTTP API to the Odoo workers. Everything about that API —
the request handler, the Bearer-token auth, the idle watchdog, the port
binding and the SSH-kwargs rehydration — is identical between the two and
lives here.

Deliberately free of ``odoo.*`` imports: the subprocess must not pay the
~10 s Odoo registry bootstrap. The concrete subprocess scripts import
this module as a flat top-level module (they put the core addon dir on
``sys.path`` first) and provide only their ``main()``: read the config,
build the right session object, then call ``run_server``.

Security properties preserved verbatim from the original per-copy code:
  * bind exclusively to 127.0.0.1 on a random kernel-assigned port;
  * one Bearer token per session, compared with ``hmac.compare_digest``;
  * ``http.server`` instantiates a handler per request, so shared state
    lives in module globals rather than on ``self``.
"""

import base64
import contextlib
import hmac
import json
import logging
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncssh

_logger = logging.getLogger("incubacloud.terminal_subprocess")


# Module-level globals populated by ``run_server`` and read by the HTTP
# handler instances. ``http.server`` instantiates a handler per request
# so state cannot be stashed on ``self``.
_SESSION = None
_AUTH_TOKEN: str = ""
_SERVER: ThreadingHTTPServer | None = None
_SHUTDOWN_LOCK = threading.Lock()


def rehydrate_ssh_kwargs(config: dict) -> dict:
    """Rebuild ``asyncssh.connect`` kwargs from a JSON-safe config.

    The parent strips the non-JSON-serialisable pieces before writing the
    config file: the ``SSHKnownHosts`` object (passed as raw text) and the
    ``client_keys`` byte lists (passed base64-encoded). This restores both.

    The ``else`` branch is load-bearing: for password-auth hosts the parent
    set ``client_keys`` to ``None`` and then stripped it, so it is absent
    here. Without restoring it to ``None`` explicitly, asyncssh treats it as
    unset and probes ``~/.ssh/id_*`` — empty/invalid inside the container —
    and fails with "Invalid private key" before it ever tries the password.
    """
    ssh_connect_kwargs = dict(config["ssh_connect_kwargs"])

    known_hosts_text = config.get("known_hosts_text") or ""
    if known_hosts_text:
        ssh_connect_kwargs["known_hosts"] = asyncssh.import_known_hosts(
            known_hosts_text,
        )

    client_keys_b64 = config.get("client_keys_b64") or []
    if client_keys_b64:
        ssh_connect_kwargs["client_keys"] = [
            base64.b64decode(k) for k in client_keys_b64
        ]
    else:
        ssh_connect_kwargs["client_keys"] = None

    return ssh_connect_kwargs


def _shutdown():
    """Tear down the HTTP server and the session, then let the process exit.

    Idempotent — the idle-watchdog thread and the ``/close`` handler can
    both race to call this; the lock ensures a single shutdown path runs.
    """
    global _SERVER, _SESSION
    with _SHUTDOWN_LOCK:
        if _SERVER is not None:
            # ``shutdown()`` must be called from a thread other than the
            # one running ``serve_forever`` — it is, because every HTTP
            # handler runs in its own thread.
            threading.Thread(target=_SERVER.shutdown, daemon=True).start()
            _SERVER = None
        if _SESSION is not None:
            with contextlib.suppress(Exception):
                _SESSION.close()
            _SESSION = None


def _watchdog():
    """Kill the subprocess once the session has been idle too long.

    ``is_expired()`` checks ``SESSION_TIMEOUT`` against the last keystroke,
    not the last output, so long-running commands do not keep the watchdog
    at bay on an abandoned tab.
    """
    while True:
        time.sleep(30)
        if _SESSION is None:
            return
        if _SESSION.is_expired() or _SESSION.closed:
            _logger.info("terminal subprocess: session expired, shutting down")
            _shutdown()
            return


class _Handler(BaseHTTPRequestHandler):
    """Loopback HTTP API in front of the one owned ``*TerminalSession``."""

    # Silence the default stderr log line per request — too noisy under
    # poll-every-second from the frontend. Errors still go through the
    # module logger below.
    def log_message(self, format, *args):  # noqa: A002
        """Suppress the stdlib per-request access log."""

    # ── Helpers ─────────────────────────────────────────────────────

    def _auth_ok(self) -> bool:
        """Constant-time Bearer-token check against ``_AUTH_TOKEN``.

        The server listens on 127.0.0.1, so a local attacker (sandbox
        escape, co-tenant) could otherwise derive the token byte-by-byte
        from the RTT timing of ``==``.
        """
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {_AUTH_TOKEN}"
        return hmac.compare_digest(header.encode(), expected.encode())

    def _write_json(self, status: int, body: dict) -> None:
        """Serialise ``body`` as a JSON HTTP response with ``status``."""
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        """Read and parse the request body as JSON, ``{}`` on any error."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _dispatch(self, method: str):
        """Authenticate, route on the path component, and run the handler."""
        if not self._auth_ok():
            self._write_json(401, {"ok": False, "error": "unauthorized"})
            return
        # ``self.path`` may contain a query string; only route on the
        # path component.
        path = self.path.split("?", 1)[0]
        handler = self._ROUTES.get((method, path))
        if handler is None:
            self._write_json(404, {"ok": False, "error": "not_found"})
            return
        try:
            handler(self)
        except Exception as exc:
            _logger.exception("terminal subprocess handler error")
            self._write_json(500, {"ok": False, "error": str(exc)})

    def do_GET(self):  # noqa: N802
        """Dispatch GET requests."""
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        """Dispatch POST requests."""
        self._dispatch("POST")

    # ── Endpoints ──────────────────────────────────────────────────

    def _handle_state(self):
        """Report the session's connection/close/idle state."""
        s = _SESSION
        if s is None:
            self._write_json(200, {
                "ok": True, "connected": False, "closed": True,
                "close_reason": "gone", "error": None, "idle_seconds": 0,
            })
            return
        self._write_json(200, {
            "ok": True,
            "connected": s.connected,
            "closed": s.closed,
            "close_reason": s.close_reason,
            "error": s.error,
            "idle_seconds": int(s.idle_seconds()),
        })

    def _handle_output(self):
        """Return buffered output chunks with seq > the ``after`` query."""
        s = _SESSION
        if s is None:
            self._write_json(200, {
                "ok": True, "chunks": [], "connected": False,
                "closed": True, "close_reason": "gone", "error": None,
                "idle_seconds": 0,
            })
            return
        # Parse ``?after=N`` from the query string.
        after = 0
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("after="):
                    with contextlib.suppress(ValueError):
                        after = int(pair.split("=", 1)[1])
                    break
        chunks = s.read_output(after)
        self._write_json(200, {
            "ok": True,
            "chunks": [
                {"seq": seq, "data": base64.b64encode(data).decode()}
                for seq, data in chunks
            ],
            "connected": s.connected,
            "closed": s.closed,
            "close_reason": s.close_reason,
            "error": s.error,
            "idle_seconds": int(s.idle_seconds()),
        })

    def _handle_input(self):
        """Feed base64-decoded bytes to the remote PTY."""
        if _SESSION is None or _SESSION.closed:
            self._write_json(200, {"ok": False, "error": "closed"})
            return
        data_b64 = self._read_json().get("data", "")
        try:
            data = base64.b64decode(data_b64)
        except Exception:
            self._write_json(400, {"ok": False, "error": "bad_data"})
            return
        _SESSION.write_input(data)
        self._write_json(200, {"ok": True})

    def _handle_resize(self):
        """Resize the remote PTY to the requested cols/rows."""
        if _SESSION is None or _SESSION.closed:
            self._write_json(200, {"ok": False, "error": "closed"})
            return
        body = self._read_json()
        cols = int(body.get("cols", 80) or 80)
        rows = int(body.get("rows", 24) or 24)
        _SESSION.resize(cols, rows)
        self._write_json(200, {"ok": True})

    def _handle_close(self):
        """Acknowledge, then tear the session and server down."""
        # Respond first, then tear down — the client has already gotten
        # what it needed and we don't want a hung wfile.write to block
        # shutdown.
        self._write_json(200, {"ok": True})
        _shutdown()

    _ROUTES = {
        ("GET", "/state"): _handle_state,
        ("GET", "/output"): _handle_output,
        ("POST", "/input"): _handle_input,
        ("POST", "/resize"): _handle_resize,
        ("POST", "/close"): _handle_close,
    }


def _bind_random_port() -> socket.socket:
    """Bind 127.0.0.1:0 and return the open socket.

    The kernel picks a free port; we hand the socket to the HTTP server
    via ``server_bind``/``server_activate`` so nothing else can grab the
    port between ``close()`` and ``bind()`` (the classic race).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def run_server(session, auth_token: str) -> int:
    """Serve the loopback API for ``session`` until it closes or times out.

    Registers ``session`` and ``auth_token`` as the module globals the
    handler reads, binds a random loopback port and prints it on the first
    line of stdout (so the parent can record it in the route row), installs
    SIGTERM/SIGINT and the idle watchdog, then serves forever. Returns the
    process exit code.
    """
    global _SESSION, _AUTH_TOKEN, _SERVER

    _SESSION = session
    _AUTH_TOKEN = auth_token

    bound = _bind_random_port()
    port = bound.getsockname()[1]

    class _Server(ThreadingHTTPServer):
        # Skip the default bind() — we already bound the socket so the
        # kernel-assigned port is stable.
        def server_bind(self):
            """Adopt the pre-bound socket instead of binding a new one."""
            self.socket = bound
            self.server_address = bound.getsockname()

        def server_activate(self):
            """Start listening on the adopted socket."""
            self.socket.listen(self.request_queue_size)

    _SERVER = _Server(("127.0.0.1", port), _Handler)

    # First line of stdout = port. Flush so the parent unblocks.
    sys.stdout.write(f"{port}\n")
    sys.stdout.flush()

    signal.signal(signal.SIGTERM, lambda *_: _shutdown())
    signal.signal(signal.SIGINT, lambda *_: _shutdown())

    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        _SERVER.serve_forever()
    finally:
        _shutdown()
    return 0
