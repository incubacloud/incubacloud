#!/usr/bin/python3 -I
"""Renew the SMTP relay's OAuth2 access token for sasl-xoauth2.

sasl-xoauth2 only knows refresh tokens. This program asks Microsoft Entra ID
for an access token with the client credentials grant and rewrites,
atomically, the token file the plugin opens on every SMTP authentication.
The plugin's own refresh is pointed at a closed local port
(``/etc/sasl-xoauth2.conf``), so the client secret never leaves this process.

Subcommands:
    run     loop forever (a supervisord program).
    status  print the status file as JSON (read by the panel's probe).

Files:
    /run/secrets/ic-relay-oauth.json   credentials, a read-only mount, root only:
        {"provider": "microsoft", "tenant_id": ..., "client_id": ...,
         "client_secret": ..., "mailbox": ...}
    /var/lib/ic-relay-oauth/token.json   the plugin's token file (root:postfix 0640)
    /var/lib/ic-relay-oauth/status.json  state for the probe (no secret, no token)

Only the standard library: the image adds nothing to docker-mailserver's Python.
"""

import grp
import http.client
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SECRETS_PATH = os.environ.get(
    "IC_RELAY_OAUTH_SECRETS", "/run/secrets/ic-relay-oauth.json"
)
STATE_DIR = os.environ.get("IC_RELAY_OAUTH_DIR", "/var/lib/ic-relay-oauth")
TOKEN_NAME = "token.json"
STATUS_NAME = "status.json"
#: Group that may read the token file: Postfix's ``smtp`` processes, which
#: load the plugin, run as ``postfix``.
TOKEN_GROUP = "postfix"
MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MS_SCOPE = "https://outlook.office365.com/.default"
#: Renew this long before expiry (2026-11-08). Microsoft's access tokens last
#: 60-90 min (``expires_in`` says so in every answer, and is what is used):
#: 20 min leave the whole backoff ladder below, twice, before the token ends
#: and the plugin's own 10 s refresh window (pointed at a closed port) is hit.
RENEW_MARGIN = 20 * 60
#: Largest ``expires_in`` taken (2026-11-09): a day. Entra ID's last 60-90 min,
#: and the value becomes the ``expiry`` the plugin trusts and the sleep until
#: the next renewal, so an absurd one would keep a dead token or silence the
#: renewer; it is a bad answer instead.
MAX_EXPIRES_IN = 24 * 60 * 60
#: Never renew more often than this, whatever ``expires_in`` says.
MIN_SLEEP = 60
#: Waits after the 1st, 2nd, 3rd and every further failed token request
#: (2026-11-08): quick enough to recover from a blip, slow enough not to
#: hammer Entra ID with a wrong secret (one request every 5 min).
BACKOFF = (30, 60, 120, 300)
HTTP_TIMEOUT = 30
#: Error messages kept in status.json and the log are cut to this length.
MAX_ERROR_LEN = 300
#: How often a missing or wrong credentials file is looked at again.
CONFIG_RECHECK = 60
#: Largest token answer read; Entra ID's are a few KiB.
MAX_RESPONSE = 64 * 1024
REQUIRED_KEYS = ("tenant_id", "client_id", "client_secret", "mailbox")


class ConfigError(Exception):
    """The credentials file is missing or unusable.

    :param code: ``config_missing``, ``config_is_directory`` or
        ``config_invalid``.
    :param message: a short explanation, never the file's content.
    """

    def __init__(self, code, message):
        super().__init__(code, message)
        self.code = code
        self.message = message


class TokenError(Exception):
    """A token request failed.

    :param code: the AADSTS code (``AADSTS7000215``), ``HTTP <status>``,
        ``network`` or ``bad_response``.
    :param message: Entra ID's ``error_description`` or the network error, cut
        to :data:`MAX_ERROR_LEN`; it never holds the secret.
    """

    def __init__(self, code, message):
        super().__init__(code, message)
        self.code = code
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the token request goes to one endpoint or fails."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Return None so urllib raises the 3xx as an ``HTTPError``."""


def token_path():
    """Return the path of the plugin's token file."""
    return os.path.join(STATE_DIR, TOKEN_NAME)


def status_path():
    """Return the path of the status file."""
    return os.path.join(STATE_DIR, STATUS_NAME)


def log(msg):
    """Write one line to stdout, which supervisord keeps.

    Never pass the secret or a token here.
    """
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def _clean(text):
    """Return ``text`` on one line and cut to :data:`MAX_ERROR_LEN`."""
    return " ".join(str(text).split())[:MAX_ERROR_LEN]


def write_atomic(path, data, mode, group=None):
    """Write ``data`` to ``path`` through a temporary file and a rename.

    The temporary file lives in the same directory, so the rename is atomic:
    a reader sees the old file or the new one, never half of one.

    :param path: final path.
    :param data: text to write.
    :param mode: permission bits of the final file.
    :param group: name of the group to own the file (the owner stays the
        writer, root in the image), or None to leave it as created.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if group:
            os.chown(tmp, -1, grp.getgrnam(group).gr_gid)
        os.chmod(tmp, mode)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_status():
    """Return the status as a dict, or a ``starting`` one if absent or unreadable."""
    try:
        with open(status_path()) as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        return {"state": "starting"}
    return status if isinstance(status, dict) else {"state": "starting"}


def write_status(**fields):
    """Merge ``fields`` into status.json and stamp ``updated_at``.

    Keys: ``state`` (``starting``, ``ok`` or ``error``), ``last_success`` and
    ``expires_at`` (epoch seconds of the last token, kept through errors so
    the probe can tell an error from a token that no longer works),
    ``error_code`` and ``error_message``.
    """
    status = read_status()
    status.update(fields)
    status["updated_at"] = int(time.time())
    os.makedirs(STATE_DIR, exist_ok=True)
    write_atomic(status_path(), json.dumps(status, sort_keys=True), 0o644)


def load_config():
    """Read and check the credentials file.

    :return: the credentials dict (``tenant_id``, ``client_id``,
        ``client_secret``, ``mailbox``, optional ``provider``).
    :raises ConfigError: when the file is missing, is a directory (Docker
        creates one when a bind-mounted file does not exist on the host at
        ``up``), or is not the expected JSON.
    """
    if os.path.isdir(SECRETS_PATH):
        raise ConfigError(
            "config_is_directory",
            "the credentials path is a directory: the file was missing on the "
            "host when the container started",
        )
    try:
        with open(SECRETS_PATH) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise ConfigError("config_missing", "no credentials file") from None
    except (OSError, ValueError):
        raise ConfigError(
            "config_invalid", "the credentials file is not readable JSON"
        ) from None
    if not isinstance(cfg, dict):
        raise ConfigError("config_invalid", "the credentials file is not an object")
    for key in REQUIRED_KEYS:
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise ConfigError("config_invalid", f"missing or empty {key}")
    if cfg.get("provider", "microsoft") != "microsoft":
        raise ConfigError("config_invalid", "unknown provider")
    return cfg


def _host_is_private(host):
    """Tell whether every address ``host`` resolves to is loopback or private.

    Link-local addresses (169.254.0.0/16, fe80::/10, where cloud metadata
    services answer) do not count as private here.

    :return: False when ``host`` is empty or does not resolve.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    addrs = {ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos}
    return bool(addrs) and all(
        a.is_loopback or (a.is_private and not a.is_link_local) for a in addrs
    )


def token_url(cfg):
    """Return the token endpoint for ``cfg``.

    ``IC_RELAY_OAUTH_TOKEN_URL`` is for the image's tests and the panel never
    writes it. It is honoured only for an ``http``/``https`` URL whose host
    resolves to loopback or a private network; any other value is ignored and
    Microsoft's endpoint is used, so plain ``http`` is possible only there.
    """
    override = os.environ.get("IC_RELAY_OAUTH_TOKEN_URL")
    if override:
        parts = urllib.parse.urlsplit(override)
        if parts.scheme in ("http", "https") and _host_is_private(parts.hostname):
            return override
        log("ignoring IC_RELAY_OAUTH_TOKEN_URL: not an http(s) URL on a "
            "loopback or private host")
    return MS_TOKEN_URL.format(tenant=urllib.parse.quote(cfg["tenant_id"], safe=""))


def _error_from_body(status, raw):
    """Turn an Entra ID error answer into a :class:`TokenError`.

    :param status: the HTTP status.
    :param raw: the body, bytes.
    """
    code, msg = f"HTTP {status}", ""
    try:
        err = json.loads(raw.decode())
    except (ValueError, UnicodeError):
        err = None
    if isinstance(err, dict):
        msg = str(err.get("error_description") or err.get("error") or "")
        codes = err.get("error_codes")
        # Only an integer is an AADSTS code: the value goes to status.json.
        if (isinstance(codes, list) and codes and isinstance(codes[0], int)
                and not isinstance(codes[0], bool)):
            code = f"AADSTS{codes[0]}"
        else:
            found = re.match(r"\s*(AADSTS\d+)", msg)
            if found:
                code = found.group(1)
    return TokenError(code, _clean(msg))


def fetch_token(cfg):
    """Ask the token endpoint for an access token (client credentials).

    TLS is checked against the system's CAs and redirects are refused.

    :return: ``(access_token, expires_in)`` with ``expires_in`` in seconds.
    :raises TokenError: with the AADSTS code, HTTP status or network error.
    """
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "scope": MS_SCOPE,
    }).encode()
    req = urllib.request.Request(
        token_url(cfg), data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read(MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE)
        except (OSError, http.client.HTTPException):
            raw = b""
        raise _error_from_body(exc.code, raw) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise TokenError("network", _clean(getattr(exc, "reason", exc))) from None
    try:
        payload = json.loads(raw.decode())
        token = payload["access_token"]
        expires_in = int(payload["expires_in"])
    except (ValueError, UnicodeError, KeyError, TypeError, OverflowError):
        raise TokenError(
            "bad_response", "token answer without access_token and expires_in"
        ) from None
    if not isinstance(token, str) or not token or expires_in <= 0:
        raise TokenError("bad_response", "empty token or expires_in not positive")
    if expires_in > MAX_EXPIRES_IN:
        raise TokenError("bad_response", f"expires_in beyond {MAX_EXPIRES_IN} s")
    return token, expires_in


def write_token(token, expiry, mailbox):
    """Write the token file sasl-xoauth2 reads on every authentication.

    Contract with the plugin (``TokenStore::Read``, release-0.28, checked
    against the real plugin): ``refresh_token`` is mandatory, any value (the
    plugin's own refresh goes to a closed port); ``expiry`` is epoch seconds,
    as a string like the plugin itself writes; ``user`` overrides the SASL
    user. Exactly these four keys: ``token_endpoint``, ``client_id``,
    ``client_secret``, ``proxy`` and ``ca_*`` in this file would override
    ``/etc/sasl-xoauth2.conf``.

    :param token: the access token.
    :param expiry: epoch seconds at which it ends.
    :param mailbox: the sender mailbox the token is for.
    """
    path = token_path()
    os.makedirs(STATE_DIR, exist_ok=True)
    os.chown(STATE_DIR, -1, grp.getgrnam(TOKEN_GROUP).gr_gid)
    # Postfix's smtp processes (group postfix) must reach the token file.
    os.chmod(STATE_DIR, 0o750)  # nosec B103
    data = json.dumps({
        "refresh_token": "unused",
        "access_token": token,
        "expiry": str(int(expiry)),
        "user": mailbox,
    })
    write_atomic(path, data, 0o640, group=TOKEN_GROUP)


def flush_queue():
    """Ask Postfix to retry the deferred mail now (after a recovery)."""
    try:
        subprocess.run(["postqueue", "-f"], timeout=30, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"postqueue -f failed: {type(exc).__name__}")


class Renewer:
    """One renewal loop's memory: failures in a row and a pending flush."""

    def __init__(self):
        """Start with no failures and a flush owed: mail may have been deferred
        before the first token."""
        self.failures = 0
        self.need_flush = True

    def step(self):
        """Run one renewal: read the credentials, ask for a token, write it.

        On a failure the previous token file is left alone, so mail keeps
        going while that token lasts. After the first success, and after one
        that follows a failure, the deferred queue is flushed.

        :return: seconds to sleep before the next step.
        """
        try:
            cfg = load_config()
        except ConfigError as exc:
            self.need_flush = True
            write_status(state="error", error_code=exc.code, error_message=exc.message)
            log(f"credentials file: {exc.code} ({exc.message})")
            return CONFIG_RECHECK
        try:
            token, expires_in = fetch_token(cfg)
        except TokenError as exc:
            self.failures += 1
            self.need_flush = True
            write_status(state="error", error_code=exc.code, error_message=exc.message)
            log(f"token request failed: {exc.code} {exc.message}")
            return BACKOFF[min(self.failures, len(BACKOFF)) - 1]
        now = int(time.time())
        expires_at = now + expires_in
        try:
            write_token(token, expires_at, cfg["mailbox"])
        except (OSError, KeyError) as exc:
            self.failures += 1
            self.need_flush = True
            write_status(state="error", error_code="token_write_failed",
                         error_message=_clean(type(exc).__name__))
            log(f"writing the token file failed: {type(exc).__name__}")
            return BACKOFF[min(self.failures, len(BACKOFF)) - 1]
        write_status(state="ok", last_success=now, expires_at=expires_at,
                     error_code="", error_message="")
        log(f"token renewed, expires in {expires_in} s")
        if self.need_flush:
            flush_queue()
            self.need_flush = False
        self.failures = 0
        return max(MIN_SLEEP, expires_in - RENEW_MARGIN)


def _prepare_wake_signal():
    """Block SIGUSR1 in the calling thread, so that only :func:`_sleep` takes it.

    A blocked signal stays pending until it is taken, so a renewal asked for
    while a step runs is not lost. No Python handler runs: a handler that took
    a lock (``threading.Event.set``) could interrupt code holding it.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})


def _sleep(seconds):
    """Sleep ``seconds``, or less if SIGUSR1 asks for a renewal now.

    Needs :func:`_prepare_wake_signal` first; takes one pending SIGUSR1.
    """
    signal.sigtimedwait({signal.SIGUSR1}, seconds)


def run():
    """Renew forever; SIGUSR1 renews at once, SIGTERM ends cleanly."""
    _prepare_wake_signal()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    os.makedirs(STATE_DIR, exist_ok=True)
    write_status(state="starting")
    renewer = Renewer()
    while True:
        try:
            wait = renewer.step()
        # Anything unforeseen is logged by its type (its text might hold a
        # value) and retried, instead of exiting into supervisord's restarts.
        except Exception as exc:  # noqa: BLE001
            log(f"unexpected error: {type(exc).__name__}")
            wait = BACKOFF[-1]
        _sleep(wait)


def main(argv):
    """Entry point: ``run`` (the default) or ``status``.

    :return: the process exit code.
    """
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "status":
        print(json.dumps(read_status(), sort_keys=True))
        return 0
    if cmd == "run":
        run()
        return 0
    print("usage: ic-relay-oauth run|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
