"""A Git server over https for fetch_repos_docker.bats.

Serves the bare repositories under <root> with ``git http-backend`` (smart
HTTP) on <port>, with <cert> and <key>. Every request is logged to <log> as
``<method> <path> auth=yes|no``: whether an ``Authorization`` header came,
never its value. A path under ``/moved/`` answers a 301 to the same path
without that prefix.

Usage: python3 git_https_server.py <root> <log> <cert> <key> <port>
"""
import http.server
import os
import ssl
import subprocess
import sys

ROOT, LOG, CERT, KEY, PORT = sys.argv[1:6]


class Handler(http.server.BaseHTTPRequestHandler):
    """Hand each request to ``git http-backend``."""

    def _serve(self):
        """Answer one request as git http-backend does, after logging it."""
        path, _sep, query = self.path.partition("?")
        with open(LOG, "a", encoding="utf-8") as log:
            auth = "yes" if self.headers.get("Authorization") else "no"
            log.write(f"{self.command} {path} auth={auth}\n")
        if path.startswith("/moved/"):
            self.send_response(301)
            self.send_header("Location", path.removeprefix("/moved"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        env = os.environ | {
            "GIT_PROJECT_ROOT": ROOT, "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path, "QUERY_STRING": query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length), "REMOTE_ADDR": "127.0.0.1",
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        out = subprocess.run(
            ["git", "http-backend"], input=body, env=env,
            capture_output=True, check=False,
        ).stdout
        head, _sep, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.split(b"\r\n"):
            name, _sep, value = line.decode().partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, *_args):
        """Say nothing on stderr."""


server = http.server.ThreadingHTTPServer(("0.0.0.0", int(PORT)), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(CERT, KEY)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
