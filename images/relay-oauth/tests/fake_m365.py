#!/usr/bin/env python3
"""Fake Microsoft 365 for the relay-oauth image tests: token endpoint + SMTP with XOAUTH2.

Run by integration.sh inside a container of the image under test (its python3 and
openssl), so it needs nothing else on the runner.

HTTP on :8080
    POST /<tenant>/oauth2/v2.0/token  client credentials grant. Valid only for
        TENANT/CLIENT_ID/CLIENT_SECRET below and scope outlook.office365.com/.default.
    GET  /control?token_mode=ok|invalid_secret|unavailable&smtp_mode=normal|reject
                  &expires_in=N&reset=1
    GET  /state   counters, issued tokens and received messages (JSON).
SMTP on :587
    STARTTLS (self-signed, /tmp/fake.crt/.key); after TLS, AUTH XOAUTH2 PLAIN LOGIN.
    XOAUTH2 accepted when user == MAILBOX and the bearer token was issued here and is
    not expired; otherwise ``535 5.7.3 Authentication unsuccessful`` like Exchange
    Online. smtp_mode=reject rejects every token (service principal without rights);
    no_starttls does not offer STARTTLS; tls_broken answers STARTTLS and then sends
    plain text instead of a TLS handshake.
"""

import base64
import json
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_SECRET = "it-secret-value"
MAILBOX = "sender@example.test"
SCOPE = "https://outlook.office365.com/.default"

LOCK = threading.Lock()
STATE = {}


def reset():
    """Reset counters and modes to their defaults."""
    with LOCK:
        STATE.clear()
        STATE.update(token_mode="ok", smtp_mode="normal", expires_in=3600,
                     token_requests=0, tokens={}, auth_ok=0, auth_fail=0,
                     auth_mechs=[], messages=[], tls_sessions=0)


def _json(handler, code, payload):
    """Send ``payload`` as JSON with HTTP ``code``."""
    body = json.dumps(payload).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class HttpHandler(BaseHTTPRequestHandler):
    """Token endpoint and control API."""

    def log_message(self, fmt, *args):
        """Quiet access log (stderr) with a prefix."""
        sys.stderr.write("[http] " + fmt % args + "\n")

    def do_GET(self):
        """Control and state endpoints."""
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/control":
            if q.get("reset"):
                reset()
            with LOCK:
                for k in ("token_mode", "smtp_mode"):
                    if k in q:
                        STATE[k] = q[k]
                if "expires_in" in q:
                    STATE["expires_in"] = int(q["expires_in"])
                if q.get("expire_all"):
                    for t in STATE["tokens"]:
                        STATE["tokens"][t] = 0
            return _json(self, 200, {"ok": True})
        if url.path == "/state":
            with LOCK:
                return _json(self, 200, STATE)
        _json(self, 404, {"error": "not_found"})

    def do_POST(self):
        """Client credentials grant, Entra ID style errors."""
        length = int(self.headers.get("Content-Length", 0))
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        with LOCK:
            STATE["token_requests"] += 1
            mode = STATE["token_mode"]
            expires_in = STATE["expires_in"]
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[1:] != ["oauth2", "v2.0", "token"] or parts[0] != TENANT:
            return _json(self, 400, {"error": "invalid_request", "error_codes": [90002],
                                     "error_description": f"AADSTS90002: Tenant '{parts[0]}' not found."})
        if mode == "unavailable":
            return _json(self, 503, {"error": "temporarily_unavailable"})
        if form.get("grant_type") != "client_credentials" or form.get("scope") != SCOPE:
            return _json(self, 400, {"error": "invalid_request", "error_codes": [900144],
                                     "error_description": "AADSTS900144: bad grant or scope"})
        if form.get("client_id") != CLIENT_ID:
            return _json(self, 400, {"error": "unauthorized_client", "error_codes": [700016],
                                     "error_description": "AADSTS700016: Application not found in the directory."})
        if mode == "invalid_secret" or form.get("client_secret") != CLIENT_SECRET:
            return _json(self, 401, {"error": "invalid_client", "error_codes": [7000215],
                                     "error_description": "AADSTS7000215: Invalid client secret provided."})
        token = "fake-at-" + uuid.uuid4().hex
        with LOCK:
            STATE["tokens"][token] = time.time() + expires_in
        _json(self, 200, {"token_type": "Bearer", "expires_in": expires_in,
                          "ext_expires_in": expires_in, "access_token": token})


class SmtpHandler(socketserver.StreamRequestHandler):
    """Minimal ESMTP server: STARTTLS, AUTH XOAUTH2, MAIL/RCPT/DATA."""

    def send(self, line):
        """Write one response line."""
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def readline(self):
        """Read one command line, without CRLF."""
        return self.rfile.readline().decode(errors="replace").rstrip("\r\n")

    def handle(self):
        """Run one SMTP session."""
        tls = authed = False
        mail_from, rcpts = None, []
        self.send("220 fake-m365 ESMTP ready")
        while True:
            line = self.readline()
            if not line:
                return
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                caps = ["250-fake-m365", "250-SIZE 157286400", "250-8BITMIME"]
                with LOCK:
                    mode = STATE["smtp_mode"]
                if tls:
                    caps.append("250-AUTH XOAUTH2 PLAIN LOGIN")
                elif mode != "no_starttls":
                    caps.append("250-STARTTLS")
                caps.append("250 ENHANCEDSTATUSCODES")
                for c in caps:
                    self.send(c)
            elif verb == "STARTTLS" and not tls:
                self.send("220 2.0.0 SMTP server ready")
                with LOCK:
                    broken = STATE["smtp_mode"] == "tls_broken"
                if broken:
                    self.send("this is not a TLS handshake")
                    return
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain("/tmp/fake.crt", "/tmp/fake.key")
                self.connection = ctx.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb")
                tls = True
                with LOCK:
                    STATE["tls_sessions"] += 1
            elif verb == "AUTH":
                args = line.split(" ")
                mech = args[1].upper() if len(args) > 1 else ""
                with LOCK:
                    STATE["auth_mechs"].append(mech)
                if not tls or mech != "XOAUTH2":
                    self.send("504 5.7.4 Unrecognized authentication type")
                    continue
                if len(args) > 2:
                    blob = args[2]
                else:
                    self.send("334 ")
                    blob = self.readline()
                try:
                    raw = base64.b64decode(blob).decode()
                    fields = dict(p.split("=", 1) for p in raw.split("\x01") if p)
                    user = fields.get("user", "")
                    token = fields.get("auth", "").removeprefix("Bearer ")
                except (ValueError, UnicodeDecodeError):
                    user = token = ""
                with LOCK:
                    exp = STATE["tokens"].get(token, 0)
                    ok = (STATE["smtp_mode"] == "normal" and user == MAILBOX
                          and exp > time.time())
                    STATE["auth_ok" if ok else "auth_fail"] += 1
                if ok:
                    authed = True
                    self.send("235 2.7.0 Authentication successful")
                else:
                    self.send("535 5.7.3 Authentication unsuccessful [FAKE.M365.TEST]")
            elif verb == "MAIL":
                if not authed:
                    self.send("530 5.7.57 Client not authenticated to send mail.")
                    continue
                mail_from, rcpts = line[10:].split(" ")[0], []
                self.send("250 2.1.0 Sender OK")
            elif verb == "RCPT":
                rcpts.append(line[8:].split(" ")[0])
                self.send("250 2.1.5 Recipient OK")
            elif verb == "DATA":
                self.send("354 Start mail input; end with <CRLF>.<CRLF>")
                body = []
                while True:
                    ln = self.readline()
                    if ln == ".":
                        break
                    body.append(ln)
                subject = next((b[9:] for b in body if b.lower().startswith("subject: ")), "")
                with LOCK:
                    STATE["messages"].append({"from": mail_from, "rcpts": rcpts,
                                              "subject": subject})
                self.send(f"250 2.0.0 OK <{uuid.uuid4().hex[:12]}@fake-m365> [Hostname=FAKE]")
            elif verb == "RSET":
                mail_from, rcpts = None, []
                self.send("250 2.0.0 Resetting")
            elif verb == "NOOP":
                self.send("250 2.0.0 OK")
            elif verb == "QUIT":
                self.send("221 2.0.0 Service closing transmission channel")
                return
            else:
                self.send("500 5.3.3 Unrecognized command")


class ThreadingTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded TCP server with address reuse."""
    allow_reuse_address = True
    daemon_threads = True


def main():
    """Start both servers and block."""
    reset()
    smtp = ThreadingTCP(("0.0.0.0", 587), SmtpHandler)
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    http = ThreadingHTTPServer(("0.0.0.0", 8080), HttpHandler)
    print("fake-m365 ready", flush=True)
    http.serve_forever()


if __name__ == "__main__":
    main()
