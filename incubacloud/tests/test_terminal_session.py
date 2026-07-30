"""Tests for the shared terminal-session machinery (Step 7A extraction).

These exercise the code the instance terminal and the host console now
share, and — crucially — the capability boundary between them:

  * the core base (``BaseTerminalSession``) never opens a PTY process at
    all; ``_open_process`` is abstract, so a subclass that forgot to
    override it raises rather than silently opening a host shell;
  * the core addon contains NO command-less ``create_process`` call — the
    host-shell capability lives only in ``incubacloud_saas_manager``;
  * the instance session's ``_open_process`` always builds a docker
    command (never a bare login shell);
  * the loopback subprocess handler enforces the Bearer token and serves
    the state/output/input/resize/close API;
  * ``rehydrate_ssh_kwargs`` restores ``client_keys=None`` for password
    -auth hosts (the bug the host console previously carried).

The session / subprocess modules are FLAT (importable without an Odoo
bootstrap): the per-session subprocess puts the addon dir on ``sys.path``
and imports them as top-level modules. We replicate that here so the
tests drive the very same import graph the runtime uses.
"""
import ast
import asyncio
import base64
import json
import os
import sys
import threading
from pathlib import Path
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncssh

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase

_CORE_DIR = get_module_path('incubacloud')
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import terminal_session_base as session_base       # noqa: E402
import terminal_session as core_session            # noqa: E402
import terminal_subprocess_base as subprocess_base  # noqa: E402

def _py_files(root):
    """Yield the paths of all non-cached ``.py`` files under *root*."""
    for dirpath, _dirs, files in os.walk(root):
        if '__pycache__' in dirpath:
            continue
        for name in files:
            if name.endswith('.py'):
                yield os.path.join(dirpath, name)


def _create_process_calls(src):
    """Return the ``ast.Call`` nodes invoking ``*.create_process(...)``.

    Parsing the AST (rather than grepping the text) ignores the token when
    it merely appears in a docstring or comment, so the capability checks
    below key on genuine calls only.
    """
    return [
        node for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'create_process'
    ]


class TestTerminalCapabilityIsolation(BaseCase):
    """The host-shell capability must not exist anywhere in core."""

    def test_base_module_has_no_create_process(self):
        """The shared base never opens a PTY — no ``create_process`` call."""
        src = Path(session_base.__file__).read_text(encoding='utf-8')
        self.assertEqual(_create_process_calls(src), [])

    def test_base_open_process_is_abstract(self):
        """A subclass that skips ``_open_process`` raises, never opens a shell."""
        inst = session_base.BaseTerminalSession.__new__(
            session_base.BaseTerminalSession,
        )
        with self.assertRaises(NotImplementedError):
            asyncio.run(inst._open_process(object()))

    def test_core_addon_has_no_commandless_create_process(self):
        """No core file opens a command-less (host) shell.

        A ``create_process`` call with no positional command is the
        host-login-shell form; it must exist only in the SaaS addon.
        """
        offenders = []
        for path in _py_files(_CORE_DIR):
            src = Path(path).read_text(encoding='utf-8')
            if any(not call.args for call in _create_process_calls(src)):
                offenders.append(path)
        self.assertEqual(
            offenders, [],
            "host-shell capability (command-less create_process) leaked "
            "into the core addon: %s" % offenders,
        )


class TestCoreOpenProcess(BaseCase):
    """The instance session confines itself to a docker command."""

    def _make(self, service='odoo', inst_dir='~/inst'):
        """Build a ``TerminalSession`` without ``__init__`` (no thread/SSH)."""
        s = core_session.TerminalSession.__new__(core_session.TerminalSession)
        s.service = service
        s.inst_dir = inst_dir
        return s

    def _run_open(self, session, container_stdout):
        """Drive ``_open_process`` against a mocked connection; return cmd."""
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.run = AsyncMock(
            return_value=SimpleNamespace(stdout=container_stdout),
        )
        conn.create_process = AsyncMock(return_value=object())
        asyncio.run(session._open_process(conn))
        self.assertTrue(conn.create_process.called)
        args, kwargs = conn.create_process.call_args
        return args, kwargs

    def test_docker_exec_when_container_resolves(self):
        """A resolved container id yields a ``docker exec`` into it."""
        args, kwargs = self._run_open(self._make(), 'cid123\n')
        self.assertTrue(args, "create_process must receive a command")
        cmd = args[0]
        self.assertIn('docker exec', cmd)
        self.assertIn('cid123', cmd)
        self.assertTrue(kwargs.get('request_pty'))

    def test_compose_exec_fallback_when_no_container(self):
        """No container id falls back to ``docker compose exec``."""
        args, _kwargs = self._run_open(self._make(service='db'), '')
        cmd = args[0]
        self.assertIn('docker compose exec', cmd)

    def test_always_passes_a_nonempty_command(self):
        """The instance session never opens a command-less shell."""
        args, _kwargs = self._run_open(self._make(), 'abc\n')
        self.assertTrue(isinstance(args[0], str) and args[0])


class TestSubprocessHandler(BaseCase):
    """The loopback HTTP API in front of a session enforces auth + routes."""

    _TOKEN = 'secret-token-xyz'

    def setUp(self):
        """Register a fake session and serve the handler on a loopback port."""
        super().setUp()
        fake = MagicMock(spec=session_base.BaseTerminalSession)
        fake.connected = True
        fake.closed = False
        fake.error = None
        fake.close_reason = None
        fake.idle_seconds.return_value = 3
        fake.read_output.return_value = [(1, b'hello'), (2, b'world')]
        self.fake = fake

        subprocess_base._SESSION = fake
        subprocess_base._AUTH_TOKEN = self._TOKEN

        self.server = ThreadingHTTPServer(
            ('127.0.0.1', 0), subprocess_base._Handler,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        """Stop the server and clear the shared module globals."""
        self.server.shutdown()
        self.server.server_close()
        subprocess_base._SESSION = None
        subprocess_base._AUTH_TOKEN = ""
        super().tearDown()

    def _call(self, method, path, token=_TOKEN, body=None):
        """Issue an HTTP request to the handler; return ``(status, dict)``."""
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, method=method, headers=headers, data=data,
        )
        try:
            # nosec B310 — the URL targets the loopback HTTP server this
            # very test spawned; no external or file:// scheme possible.
            with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_rejects_wrong_token(self):
        """A bad Bearer token is refused with 401."""
        status, body = self._call('GET', '/state', token='nope')
        self.assertEqual(status, 401)
        self.assertFalse(body['ok'])

    def test_rejects_missing_token(self):
        """A missing Authorization header is refused with 401."""
        status, _body = self._call('GET', '/state', token=None)
        self.assertEqual(status, 401)

    def test_state(self):
        """``/state`` reports the session's live flags."""
        status, body = self._call('GET', '/state')
        self.assertEqual(status, 200)
        self.assertTrue(body['connected'])
        self.assertEqual(body['idle_seconds'], 3)

    def test_output_is_base64(self):
        """``/output`` returns the buffered chunks base64-encoded."""
        status, body = self._call('GET', '/output?after=0')
        self.assertEqual(status, 200)
        datas = [base64.b64decode(c['data']) for c in body['chunks']]
        self.assertEqual(datas, [b'hello', b'world'])

    def test_input_forwards_bytes(self):
        """``/input`` decodes base64 and forwards the bytes to the session."""
        payload = base64.b64encode(b'ls -la\n').decode()
        status, body = self._call('POST', '/input', body={'data': payload})
        self.assertEqual(status, 200)
        self.fake.write_input.assert_called_once_with(b'ls -la\n')

    def test_resize_forwards_dims(self):
        """``/resize`` forwards the cols/rows to the session."""
        status, _body = self._call(
            'POST', '/resize', body={'cols': 120, 'rows': 40},
        )
        self.assertEqual(status, 200)
        self.fake.resize.assert_called_once_with(120, 40)

    def test_unknown_route_404(self):
        """An unrouted path returns 404 (after passing auth)."""
        status, _body = self._call('GET', '/nope')
        self.assertEqual(status, 404)


class TestRehydrateSshKwargs(BaseCase):
    """Rebuilding asyncssh kwargs from the JSON-safe subprocess config."""

    def test_password_auth_sets_client_keys_none(self):
        """No key material → ``client_keys`` is explicitly None (the fix).

        Without this, asyncssh probes ``~/.ssh/id_*`` and fails before it
        ever tries the password — the bug the host console used to carry.
        """
        out = subprocess_base.rehydrate_ssh_kwargs({
            'ssh_connect_kwargs': {
                'host': 'h', 'username': 'u', 'password': 'p',
            },
        })
        self.assertIn('client_keys', out)
        self.assertIsNone(out['client_keys'])

    def test_key_auth_decodes_client_keys(self):
        """Base64 key material is decoded back to raw bytes."""
        out = subprocess_base.rehydrate_ssh_kwargs({
            'ssh_connect_kwargs': {'host': 'h', 'username': 'u'},
            'client_keys_b64': [base64.b64encode(b'PRIVATEKEY').decode()],
        })
        self.assertEqual(out['client_keys'], [b'PRIVATEKEY'])

    def test_known_hosts_absent_when_empty(self):
        """An empty ``known_hosts_text`` leaves ``known_hosts`` unset."""
        out = subprocess_base.rehydrate_ssh_kwargs({
            'ssh_connect_kwargs': {'host': 'h'},
            'known_hosts_text': '',
        })
        self.assertNotIn('known_hosts', out)
