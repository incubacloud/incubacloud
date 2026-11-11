"""Tests for the shared terminal-session machinery (Step 7A extraction).

These exercise the code the instance terminal and the host console now
share, and — crucially — the capability boundary between them:

  * the core base (``BaseTerminalSession``) never opens a PTY process at
    all; ``_open_process`` is abstract, so a subclass that forgot to
    override it raises rather than silently opening a host shell;
  * exactly one file in the addon holds a command-less ``create_process``
    call — the host session, behind the manager-only endpoint — so the
    host-shell capability cannot spread to any other code path;
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
import tempfile
import threading
import time
from pathlib import Path
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncssh

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase
from odoo.addons.incubacloud.controllers.terminal_proxy_mixin import (
    spawn_subprocess,
)

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
    """The host-shell capability lives in exactly one place."""

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

    def test_only_the_host_session_opens_a_commandless_shell(self):
        """Only ``host_terminal_session.py`` opens a command-less shell.

        A ``create_process`` call with no positional command is the
        host-login-shell form. The host shell's endpoint is reserved to
        whoever manages hosts; the same call anywhere else would hand
        root on the machine to a path nobody reviewed as such.
        """
        commandless = []
        for path in _py_files(_CORE_DIR):
            src = Path(path).read_text(encoding='utf-8')
            if any(not call.args for call in _create_process_calls(src)):
                commandless.append(os.path.relpath(path, _CORE_DIR))
        self.assertEqual(
            commandless, ['host_terminal_session.py'],
            "a command-less create_process (a host login shell) must live "
            "only in host_terminal_session.py; found in: %s" % commandless,
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


class TestPumpEndsWithTheShell(BaseCase):
    """``exit`` in the shell ends the session, and the page says so."""

    def _session(self):
        """A ``TerminalSession`` with its buffers, without thread or SSH."""
        s = core_session.TerminalSession.__new__(core_session.TerminalSession)
        s._closed = False
        s._close_reason = None
        s._input_queue = []
        s._input_lock = threading.Lock()
        s._output_buffer = []
        s._output_lock = threading.Lock()
        s._output_cond = threading.Condition(s._output_lock)
        s._output_seq = 0
        return s

    def test_the_session_closes_when_the_remote_shell_exits(self):
        s = self._session()
        process = MagicMock(spec=asyncssh.SSHClientProcess)
        process.stdout = MagicMock(spec=asyncssh.SSHReader)
        process.stdout.read = AsyncMock(side_effect=[b'exit\r\n', b''])
        process.stdin = MagicMock(spec=asyncssh.SSHWriter)
        asyncio.run(asyncio.wait_for(s._pump(process), timeout=5))
        self.assertTrue(s.closed)
        self.assertIsNone(s.close_reason)
        self.assertEqual(s._output_buffer, [(1, b'exit\r\n')])


class TestSessionBuffers(BaseCase):
    """What the page reads and writes, without a thread or SSH.

    The page answers what is typed with the echo that follows it, waiting
    for it on the session, and sends a batch again when it got no answer:
    the session applies each numbered batch once.
    """

    def _session(self):
        """A session built by the base's ``__init__``, never started."""
        return session_base.BaseTerminalSession('buffers', {'host': '192.0.2.1'})

    def test_a_read_returns_at_once_when_there_is_output(self):
        s = self._session()
        s._append_output(b'ready')
        started = time.monotonic()
        self.assertEqual(s.read_output(0, wait=0.5), [(1, b'ready')])
        self.assertLess(time.monotonic() - started, 0.1)

    def test_a_read_waits_for_output_and_returns_when_it_comes(self):
        s = self._session()
        threading.Timer(0.05, s._append_output, args=(b'echo',)).start()
        started = time.monotonic()
        self.assertEqual(s.read_output(0, wait=0.5), [(1, b'echo')])
        self.assertLess(time.monotonic() - started, 0.4)

    def test_the_wait_is_capped(self):
        """It holds an Odoo worker: no caller makes it a long poll."""
        s = self._session()
        started = time.monotonic()
        self.assertEqual(s.read_output(0, wait=30), [])
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, session_base.MAX_OUTPUT_WAIT - 0.05)
        self.assertLess(elapsed, session_base.MAX_OUTPUT_WAIT + 0.3)

    def test_a_closed_session_does_not_wait(self):
        s = self._session()
        s.close()
        started = time.monotonic()
        self.assertEqual(s.read_output(0, wait=0.5), [])
        self.assertLess(time.monotonic() - started, 0.1)

    def test_a_numbered_batch_reaches_the_shell_once(self):
        s = self._session()
        self.assertTrue(s.write_input(b'ls', seq=1, stream='page-a'))
        self.assertFalse(s.write_input(b'ls', seq=1, stream='page-a'))
        self.assertTrue(s.write_input(b' -l', seq=2, stream='page-a'))
        self.assertEqual(s._input_queue, [b'ls', b' -l'])

    def test_a_resize_reaches_the_shell(self):
        """It never did: the method is not a coroutine (11-nov-2026)."""
        s = self._session()
        s._process = MagicMock(spec=asyncssh.SSHClientProcess)
        s._loop = asyncio.new_event_loop()
        runner = threading.Thread(target=s._loop.run_forever, daemon=True)
        runner.start()
        try:
            s.resize(161, 40)
            done = threading.Event()
            s._loop.call_soon_threadsafe(done.set)
            self.assertTrue(done.wait(5))
        finally:
            s._loop.call_soon_threadsafe(s._loop.stop)
            runner.join(5)
            s._loop.close()
        s._process.change_terminal_size.assert_called_once_with(161, 40)

    def test_another_page_numbers_from_the_start(self):
        """A page opened again starts at 1 and is not taken for a resend."""
        s = self._session()
        s.write_input(b'a', seq=5, stream='page-a')
        self.assertTrue(s.write_input(b'b', seq=1, stream='page-b'))
        self.assertTrue(s.write_input(b'c'))
        self.assertEqual(s._input_queue, [b'a', b'b', b'c'])


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
        self.fake.write_input.assert_called_once_with(
            b'ls -la\n', seq=None, stream=None,
        )

    def test_numbered_input_says_when_it_was_a_resend(self):
        """A batch the session already applied is acknowledged as such."""
        self.fake.write_input.side_effect = [True, False]
        body = {'data': base64.b64encode(b'x').decode(), 'seq': 7, 'stream': 'p1'}
        _status, first = self._call('POST', '/input', body=body)
        _status, again = self._call('POST', '/input', body=body)
        self.assertEqual((first['ok'], first['duplicate']), (True, False))
        self.assertEqual((again['ok'], again['duplicate']), (True, True))
        self.fake.write_input.assert_called_with(b'x', seq=7, stream='p1')

    def test_output_passes_the_wait_in_seconds(self):
        """``?wait=`` is in ms on the wire, seconds for the session."""
        self._call('GET', '/output?after=4&wait=200')
        self.fake.read_output.assert_called_once_with(4, wait=0.2)
        self.fake.read_output.reset_mock()
        self._call('GET', '/output?after=4')
        self.fake.read_output.assert_called_once_with(4, wait=0.0)

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


class TestTerminalSpawnSecrets(BaseCase):
    """The parent passes terminal secrets only through its protected file."""

    @staticmethod
    def _script(path, body):
        """Write a short child script used to exercise the real spawn path."""
        path.write_text(body, encoding='utf-8')

    def test_token_is_not_in_argv_and_is_available_to_the_child(self):
        """The child gets the token from config rather than its argv."""
        token = 'terminal-token-that-must-not-be-in-ps'
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            result = directory / 'result.json'
            script = directory / 'child.py'
            self._script(
                script,
                'import json\n'
                'import sys\n'
                'from pathlib import Path\n'
                'config_index = sys.argv.index("--config-file")\n'
                'config_path = Path(sys.argv[config_index + 1])\n'
                'config = json.loads(config_path.read_text())\n'
                f'result = Path({str(result)!r})\n'
                'result.write_text(json.dumps({"argv": sys.argv, '
                '"token": config["auth_token"]}))\n'
                'print("32123", flush=True)\n',
            )
            port, process_id = spawn_subprocess(
                'session-id', token, {'inst_dir': '/tmp/instance'},
                subprocess_path=str(script), core_dir=str(directory),
                tmp_prefix='ic-terminal-test-', fail_label='Terminal',
            )
            payload = json.loads(result.read_text(encoding='utf-8'))
            os.kill(process_id, 9)

        self.assertEqual(port, 32123)
        self.assertEqual(payload['token'], token)
        self.assertNotIn(token, payload['argv'])
        self.assertNotIn('--auth-token', payload['argv'])

    def test_unserializable_config_is_removed_before_spawn(self):
        """A JSON failure does not leave a credential file behind."""
        prefix = f'ic-terminal-test-json-{os.getpid()}-'
        temp_dir = Path(tempfile.gettempdir())
        self.assertEqual(list(temp_dir.glob(f'{prefix}*.json')), [])

        with self.assertRaises(TypeError):
            spawn_subprocess(
                'session-id', 'token', {'value': object()},
                subprocess_path='not-reached.py', core_dir='/tmp',
                tmp_prefix=prefix, fail_label='Terminal',
            )

        self.assertEqual(list(temp_dir.glob(f'{prefix}*.json')), [])

    def test_failed_child_does_not_leave_credential_file(self):
        """A child that misses the startup protocol triggers cleanup."""
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            marker = directory / 'config-path'
            script = directory / 'child.py'
            self._script(
                script,
                'import sys\n'
                'from pathlib import Path\n'
                'config_index = sys.argv.index("--config-file")\n'
                f'Path({str(marker)!r}).write_text(sys.argv[config_index + 1])\n',
            )
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                spawn_subprocess(
                    'session-id', 'token', {'inst_dir': '/tmp/instance'},
                    subprocess_path=str(script), core_dir=str(directory),
                    tmp_prefix='ic-terminal-test-', fail_label='Terminal',
                )

            config_path = Path(marker.read_text(encoding='utf-8'))
            self.assertFalse(config_path.exists())
