"""Shared controller machinery for the terminal / host-console proxies.

Both ``incubacloud.controllers.terminal`` and
``incubacloud_saas_manager.controllers.host_terminal`` are thin HTTP
proxies in front of a per-session subprocess: they look the session's
subprocess up in a route model and forward the request to its loopback
port. The lookup/forward logic (``_proxy``), the spawn+port-read dance
(``spawn_subprocess``) and the timed stdout read (``_readline_with_timeout``)
are identical between them and live here.

Each ``@http.route`` needs a static path, so the route methods themselves
stay in the concrete controllers; they mix in ``TerminalProxyMixin`` and
set ``_ROUTE_MODEL`` to their own route model.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

from odoo import _
from odoo.http import request

from ..terminal_session_base import SESSION_TIMEOUT

# Subprocess HTTP timeout: the subprocess only talks to in-memory buffers
# and an SSH socket — 5 s is generous. A longer timeout would let a hung
# subprocess wedge an Odoo worker on every poll.
PROXY_TIMEOUT = 5.0

# How long we wait for the subprocess's first line of stdout (the port
# number). It only needs to open a socket and print a number; 10 s is
# plenty and we do not want to hang the UI.
SPAWN_TIMEOUT = 10.0


def _closed_sentinel():
    """Return the API payload that reads as a timed-out/gone session."""
    return {
        'ok': True, 'chunks': [], 'connected': False,
        'closed': True, 'close_reason': 'timeout', 'error': None,
        'idle_seconds': SESSION_TIMEOUT,
    }


class TerminalProxyMixin:
    """Mixin adding subprocess-proxy behaviour to a terminal controller.

    The concrete controller must set ``_ROUTE_MODEL`` to the name of its
    route model (``cloud.terminal.route`` / ``cloud.host.terminal.route``).
    """

    # Overridden by each concrete controller.
    _ROUTE_MODEL = None

    def _proxy(self, session_id, method, sub_path, body=None, params=None):
        """Proxy a request to the subprocess owning *session_id*.

        Returns the parsed JSON dict from the subprocess, or a payload
        shaped like the terminal API's "closed" state when the route is
        gone (subprocess died or was never created).
        """
        route = request.env[self._ROUTE_MODEL]._resolve(session_id)
        if not route:
            return _closed_sentinel()
        # Ownership: the audit row carries the user binding; the route row
        # carries the runtime binding. Both must match so a second user on
        # the same Odoo cannot hijack a session by guessing the id.
        if route.user_id.id != request.env.user.id:
            return {'ok': False, 'error': _('Session not found')}

        url = f"http://127.0.0.1:{route.port}{sub_path}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(
            url,
            method=method,
            headers={
                'Authorization': f"Bearer {route.auth_token}",
                'Content-Type': 'application/json',
            },
            data=json.dumps(body).encode('utf-8') if body is not None else None,
        )
        try:
            with urllib.request.urlopen(  # nosec B310 — 127.0.0.1 only
                req, timeout=PROXY_TIMEOUT,
            ) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.URLError:
            # Subprocess just died between _resolve() and urlopen — clean
            # the stale row so the next poll reports closed.
            route.unlink()
            return _closed_sentinel()


def _readline_with_timeout(proc, timeout):
    """Read one line from ``proc.stdout`` with a timeout.

    ``subprocess.Popen.stdout`` is a regular binary file so there is no
    native timeout. We wrap the read in a thread and give up if the thread
    is still alive when the deadline hits.
    """
    result = {'line': b''}

    def _read():
        with contextlib.suppress(Exception):
            result['line'] = proc.stdout.readline()

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return b''
    return result['line']


def spawn_subprocess(session_id, auth_token, config, *,
                     subprocess_path, core_dir, tmp_prefix, fail_label):
    """Spawn a terminal subprocess and return ``(port, pid)``.

    ``subprocess_path`` is the absolute path to the concrete subprocess
    script (it differs per addon); ``core_dir`` is the core addon directory
    exported to the child as ``INCUBACLOUD_CORE_DIR`` so its flat imports
    resolve. SSH material and the loopback Bearer token are passed through a
    short-lived temp file (mode 0600), never a CLI argument, since args are
    visible in ``ps aux``. The child deletes the file immediately after
    reading; this parent also removes it if spawning cannot complete.
    """
    # The config file lives only long enough for the subprocess to read
    # it. ``delete=False`` because ``NamedTemporaryFile`` would otherwise
    # try to close+unlink while we still need the path.
    tmp_path = None

    def _remove_temp_config():
        """Remove the credential file, regardless of how spawn failed."""
        if tmp_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)

    def _stop_failed_process(proc):
        """Reap a child that did not complete the startup protocol."""
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1)
        with contextlib.suppress(Exception):
            proc.stdout.close()

    config = dict(config, auth_token=auth_token)
    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.json',
        prefix=tmp_prefix,
        delete=False,
    )
    tmp_path = tmp.name
    try:
        os.chmod(tmp.name, 0o600)
        json.dump(config, tmp)
        tmp.flush()
    except Exception:
        _remove_temp_config()
        raise
    finally:
        tmp.close()

    # Invoke as a plain script (not ``-m``): running the addon as a module
    # executes its ``__init__`` and pulls ``odoo.http`` into a bare
    # interpreter → circular ImportError. ``-u`` forces line-buffered
    # stdout so we can read the port line off the binary pipe.
    argv = [
        sys.executable, '-u', subprocess_path,
        '--session-id', session_id,
        '--config-file', tmp.name,
    ]
    env = os.environ | {'INCUBACLOUD_CORE_DIR': core_dir}
    # ``start_new_session=True`` detaches from the Odoo worker's process
    # group so the subprocess survives worker restarts — the entire point
    # of this design. stdin is closed so any accidental ``input()`` inside
    # the subprocess fails fast instead of hanging.
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,              # inherit Odoo's stderr for logs
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except Exception:
        _remove_temp_config()
        raise

    # The subprocess prints its port on the first line of stdout and then
    # starts serving. If it dies before printing, readline returns b'' and
    # we treat it as a spawn failure.
    first_line = _readline_with_timeout(proc, SPAWN_TIMEOUT)
    if not first_line:
        _stop_failed_process(proc)
        _remove_temp_config()
        raise RuntimeError(f"{fail_label} failed to start")

    try:
        port = int(first_line.strip())
    except ValueError:
        _stop_failed_process(proc)
        _remove_temp_config()
        raise RuntimeError(
            f"{fail_label} printed invalid port: {first_line!r}"
        ) from None
    return port, proc.pid
