"""Shared fixtures for the relay-oauth tests: the renewer module and a fake token endpoint.

Only the standard library and pytest: ``python3 -m pytest images/relay-oauth/tests``.
"""

import grp
import importlib.util
import json
import os
import pathlib
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

IMAGE_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_renewer():
    """Import ``ic_relay_oauth.py`` as a fresh module object."""
    spec = importlib.util.spec_from_file_location(
        "ic_relay_oauth_under_test", IMAGE_DIR / "ic_relay_oauth.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTokenServer:
    """A token endpoint answering like Entra ID's, one scripted answer at a time.

    ``answers`` is a list consumed in order (the last one repeats): each item is
    ``(status, body)``, with ``body`` a dict sent as JSON or bytes sent as is,
    or the string ``"hang"`` to wait past the client's timeout. Every request's
    form is kept in ``requests``.
    """

    def __init__(self):
        """Start the server on a free loopback port, in a daemon thread."""
        self.answers = [(200, {"access_token": "at-1", "expires_in": 3600})]
        self.requests = []
        self.paths = []
        self.hang_seconds = 2.0
        server = self

        class Handler(BaseHTTPRequestHandler):
            """Serve POST /<tenant>/oauth2/v2.0/token with the next answer."""

            def log_message(self, fmt, *args):
                """Keep the test output quiet."""

            def do_POST(self):
                """Record the form and send the next scripted answer."""
                length = int(self.headers.get("Content-Length", 0))
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                server.requests.append(form)
                server.paths.append(self.path)
                answer = server.answers.pop(0) if len(server.answers) > 1 else server.answers[0]
                if answer == "hang":
                    time.sleep(server.hang_seconds)
                    return
                status, body = answer
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                if status in (301, 302, 303, 307, 308):
                    self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/tenant-x/oauth2/v2.0/token"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def close(self):
        """Stop the server."""
        self.httpd.shutdown()
        self.httpd.server_close()


SECRET = "test-secret-test"  # low entropy on purpose: a fake, not a gitleaks find
CREDENTIALS = {
    "provider": "microsoft",
    "tenant_id": "11111111-2222-3333-4444-555555555555",
    "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "client_secret": SECRET,
    "mailbox": "sender@example.test",
}


@pytest.fixture
def token_server():
    """A running :class:`FakeTokenServer`, stopped after the test."""
    server = FakeTokenServer()
    yield server
    server.close()


@pytest.fixture
def renewer(tmp_path, monkeypatch, token_server):
    """The renewer module pointed at a temporary state directory and the fake endpoint.

    The token file's group is the test user's own (``postfix`` exists only in
    the image), ``postqueue -f`` is replaced by a counter (``renewer.flushes``)
    and the credentials file holds :data:`CREDENTIALS`.
    """
    module = _load_renewer()
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps(CREDENTIALS))
    state = tmp_path / "state"
    monkeypatch.setattr(module, "SECRETS_PATH", str(secrets))
    monkeypatch.setattr(module, "STATE_DIR", str(state))
    monkeypatch.setattr(module, "TOKEN_GROUP", grp.getgrgid(os.getgid()).gr_name)
    monkeypatch.setattr(module, "HTTP_TIMEOUT", 0.5)
    monkeypatch.setenv("IC_RELAY_OAUTH_TOKEN_URL", token_server.url)
    module.flushes = 0

    def fake_flush():
        """Count the flushes instead of calling postqueue."""
        module.flushes += 1

    monkeypatch.setattr(module, "flush_queue", fake_flush)
    module.secrets_file = secrets
    return module
