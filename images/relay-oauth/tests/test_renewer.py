"""The renewer against a fake token endpoint: what it writes, keeps, waits and never says."""

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest
from conftest import CREDENTIALS, SECRET


def _token(renewer):
    """Return the token file's content as a dict."""
    with open(renewer.token_path()) as fh:
        return json.load(fh)


def _status(renewer):
    """Return the status file's content as a dict."""
    with open(renewer.status_path()) as fh:
        return json.load(fh)


def _aadsts(code, text):
    """Return an Entra ID error body for ``AADSTS<code>``."""
    return {
        "error": "invalid_client",
        "error_description": f"AADSTS{code}: {text} Trace ID: x Correlation ID: y",
        "error_codes": [code],
    }


# --- Success ------------------------------------------------------------------


def test_success_writes_the_plugin_token_file(renewer, token_server):
    """A token answer becomes the four-key file sasl-xoauth2 reads, with expiry from expires_in."""
    token_server.answers = [(200, {"access_token": "at-abc", "expires_in": 4000})]
    before = int(time.time())
    wait = renewer.Renewer().step()
    data = _token(renewer)
    assert set(data) == {"refresh_token", "access_token", "expiry", "user"}
    assert data["access_token"] == "at-abc"
    assert data["user"] == CREDENTIALS["mailbox"]
    assert data["refresh_token"]
    assert isinstance(data["expiry"], str)
    assert before + 4000 <= int(data["expiry"]) <= int(time.time()) + 4000
    assert wait == 4000 - renewer.RENEW_MARGIN


def test_the_request_is_a_client_credentials_grant(renewer, token_server):
    """The form carries the grant, the app's credentials and Exchange Online's scope."""
    renewer.Renewer().step()
    form = token_server.requests[0]
    assert form == {
        "grant_type": "client_credentials",
        "client_id": CREDENTIALS["client_id"],
        "client_secret": SECRET,
        "scope": "https://outlook.office365.com/.default",
    }


def test_token_file_and_directory_permissions(renewer):
    """Token file 0640 in a 0750 directory, both of the token group; no temporary left."""
    renewer.Renewer().step()
    path = renewer.token_path()
    st = os.stat(path)
    assert stat.S_IMODE(st.st_mode) == 0o640
    assert st.st_gid == os.getgid()
    assert stat.S_IMODE(os.stat(renewer.STATE_DIR).st_mode) == 0o750
    assert sorted(os.listdir(renewer.STATE_DIR)) == ["status.json", "token.json"]


def test_status_after_success(renewer, token_server):
    """status.json says ok, when it succeeded and when the token ends, and no error."""
    token_server.answers = [(200, {"access_token": "at-1", "expires_in": 3600})]
    renewer.Renewer().step()
    status = _status(renewer)
    assert status["state"] == "ok"
    assert status["expires_at"] - status["last_success"] == 3600
    assert status["error_code"] == ""
    assert status["error_message"] == ""


@pytest.mark.parametrize(
    ("expires_in", "wait"), [(3600, 2400), (5400, 4200), (1300, 100), (600, 60), (61, 60)]
)
def test_next_renewal_follows_expires_in(renewer, token_server, expires_in, wait):
    """The renewer sleeps until 20 min before the end, never less than 60 s."""
    token_server.answers = [(200, {"access_token": "at", "expires_in": expires_in})]
    assert renewer.Renewer().step() == wait


def test_an_expires_in_of_a_day_is_the_most_accepted(renewer, token_server):
    """A day is taken as sent: only what is beyond it is a bad answer."""
    token_server.answers = [(200, {"access_token": "at", "expires_in": 86400})]
    assert renewer.Renewer().step() == 86400 - renewer.RENEW_MARGIN
    assert _status(renewer)["state"] == "ok"


def test_a_huge_expires_in_writes_no_token(renewer, token_server):
    """An absurd expiry never reaches the token file the plugin trusts."""
    token_server.answers = [(200, {"access_token": "at", "expires_in": 10**12})]
    renewer.Renewer().step()
    assert not os.path.exists(renewer.token_path())


# --- Failures -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "code", "message"),
    [
        ((401, _aadsts(7000215, "Invalid client secret provided.")),
         "AADSTS7000215", "Invalid client secret provided."),
        ((401, _aadsts(7000222, "The provided client secret keys are expired.")),
         "AADSTS7000222", "The provided client secret keys are expired."),
        ((400, _aadsts(700016, "Application not found in the directory.")),
         "AADSTS700016", "Application not found"),
        ((400, {"error": "invalid_request",
                "error_description": "AADSTS90002: Tenant 'x' not found."}),
         "AADSTS90002", "Tenant 'x' not found."),
        ((429, {"error": "too_many_requests"}), "HTTP 429", "too_many_requests"),
        # error_codes[0] goes to status.json: only an integer is an AADSTS code.
        ((400, {"error": "invalid_request", "error_codes": ["AADSTS1; <b>"]}),
         "HTTP 400", "invalid_request"),
        ((400, {"error": "invalid_request", "error_codes": [{"a": 1}]}),
         "HTTP 400", "invalid_request"),
        ((400, {"error": "invalid_request", "error_codes": [True]}),
         "HTTP 400", "invalid_request"),
        ((503, b"<html>down</html>"), "HTTP 503", ""),
        ((200, b"{not json"), "bad_response", "access_token"),
        ((200, {"token_type": "Bearer"}), "bad_response", "access_token"),
        ((200, {"access_token": "", "expires_in": 3600}), "bad_response", "empty"),
        ((200, {"access_token": "at", "expires_in": 0}), "bad_response", "positive"),
        ((200, {"access_token": "at", "expires_in": 10**12}), "bad_response", "expires_in"),
        ((200, {"access_token": "at", "expires_in": 86401}), "bad_response", "expires_in"),
        ((200, b'{"access_token": "at", "expires_in": Infinity}'), "bad_response", "access_token"),
        ((302, b""), "HTTP 302", ""),
        ("hang", "network", ""),
    ],
)
def test_failure_codes(renewer, token_server, answer, code, message):
    """Each kind of failure lands in status.json as its code and message, state error."""
    token_server.answers = [answer]
    renewer.Renewer().step()
    status = _status(renewer)
    assert status["state"] == "error"
    assert status["error_code"] == code
    assert message in status["error_message"]
    assert len(status["error_message"]) <= renewer.MAX_ERROR_LEN
    assert "\n" not in status["error_message"]


def test_a_redirect_is_not_followed(renewer, token_server):
    """A 3xx is a failure: the secret is never sent to the Location."""
    token_server.answers = [(307, b"")]
    renewer.Renewer().step()
    assert len(token_server.requests) == 1
    assert _status(renewer)["error_code"] == "HTTP 307"


def test_the_message_is_cut_and_kept_on_one_line(renewer, token_server):
    """A long or multi-line error_description is flattened and cut to 300 characters."""
    token_server.answers = [(400, {"error_description": "AADSTS1:\n" + "x" * 1000,
                                   "error_codes": [1]})]
    renewer.Renewer().step()
    message = _status(renewer)["error_message"]
    assert len(message) == renewer.MAX_ERROR_LEN
    assert "\n" not in message


def test_backoff_grows_and_stops_at_five_minutes(renewer, token_server):
    """Failures in a row wait 30 s, 1, 2 and then 5 min each."""
    token_server.answers = [(503, b"")]
    r = renewer.Renewer()
    assert [r.step() for _ in range(6)] == [30, 60, 120, 300, 300, 300]


def test_success_resets_the_backoff(renewer, token_server):
    """After a success, the next failure waits 30 s again."""
    token_server.answers = [(503, b""), (503, b""), (200, {"access_token": "a",
                                                          "expires_in": 3600}), (503, b"")]
    r = renewer.Renewer()
    assert [r.step() for _ in range(4)] == [30, 60, 2400, 30]


def test_a_failure_keeps_the_previous_token(renewer, token_server):
    """While the token lasts, a failed renewal leaves the file and expires_at as they were."""
    token_server.answers = [(200, {"access_token": "at-good", "expires_in": 3600}),
                            (401, _aadsts(7000215, "Invalid client secret provided."))]
    r = renewer.Renewer()
    r.step()
    before = _token(renewer)
    expires_at = _status(renewer)["expires_at"]
    r.step()
    assert _token(renewer) == before
    status = _status(renewer)
    assert status["state"] == "error"
    assert status["expires_at"] == expires_at
    assert status["last_success"] <= expires_at


# --- Flushing the deferred mail ----------------------------------------------------


def test_flush_after_the_first_token(renewer):
    """The first success flushes the queue (mail deferred before any token); later ones do not."""
    r = renewer.Renewer()
    r.step()
    r.step()
    assert renewer.flushes == 1


def test_flush_after_a_recovery(renewer, token_server):
    """A success that follows a failure flushes again."""
    ok = (200, {"access_token": "a", "expires_in": 3600})
    token_server.answers = [ok, (503, b""), ok, ok]
    r = renewer.Renewer()
    for _ in range(4):
        r.step()
    assert renewer.flushes == 2


def test_no_flush_while_failing(renewer, token_server):
    """Nothing is flushed when no token came."""
    token_server.answers = [(503, b"")]
    r = renewer.Renewer()
    r.step()
    r.step()
    assert renewer.flushes == 0


# --- Credentials file ----------------------------------------------------------------


def test_missing_credentials(renewer, token_server):
    """No file: config_missing, nothing asked, looked at again in 60 s."""
    renewer.secrets_file.unlink()
    assert renewer.Renewer().step() == renewer.CONFIG_RECHECK
    assert _status(renewer)["error_code"] == "config_missing"
    assert token_server.requests == []


def test_credentials_path_is_a_directory(renewer, token_server):
    """Docker made a directory of a missing bind-mounted file: config_is_directory says so."""
    renewer.secrets_file.unlink()
    renewer.secrets_file.mkdir()
    assert renewer.Renewer().step() == renewer.CONFIG_RECHECK
    status = _status(renewer)
    assert status["error_code"] == "config_is_directory"
    assert "directory" in status["error_message"]
    assert token_server.requests == []


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({**CREDENTIALS, "client_secret": ""}),
        json.dumps({k: v for k, v in CREDENTIALS.items() if k != "mailbox"}),
        json.dumps({**CREDENTIALS, "tenant_id": 5}),
        json.dumps({**CREDENTIALS, "provider": "google"}),
    ],
)
def test_invalid_credentials(renewer, token_server, content):
    """A file that is not the expected object: config_invalid, nothing asked."""
    renewer.secrets_file.write_text(content)
    assert renewer.Renewer().step() == renewer.CONFIG_RECHECK
    assert _status(renewer)["error_code"] == "config_invalid"
    assert token_server.requests == []


def test_provider_may_be_left_out(renewer):
    """Without ``provider`` the file is read as Microsoft's."""
    renewer.secrets_file.write_text(
        json.dumps({k: v for k, v in CREDENTIALS.items() if k != "provider"})
    )
    renewer.Renewer().step()
    assert _status(renewer)["state"] == "ok"


def test_credentials_are_read_again_on_every_step(renewer):
    """A credentials file that appears later is picked up, and the queue is flushed."""
    renewer.secrets_file.unlink()
    r = renewer.Renewer()
    r.step()
    renewer.secrets_file.write_text(json.dumps(CREDENTIALS))
    r.step()
    assert _status(renewer)["state"] == "ok"
    assert renewer.flushes == 1


# --- Token endpoint override (tests only) -------------------------------------------------


@pytest.mark.parametrize(
    ("override", "honoured"),
    [
        ("http://127.0.0.1:8080/t/oauth2/v2.0/token", True),
        ("http://localhost:8080/t/oauth2/v2.0/token", True),
        ("http://10.1.2.3/t", True),
        ("https://192.168.1.10/t", True),
        ("http://[::1]:8080/t", True),
        ("http://93.184.216.34/t", False),
        ("https://8.8.8.8/t", False),
        ("http://169.254.169.254/latest/meta-data", False),
        ("file:///etc/passwd", False),
        ("ftp://127.0.0.1/t", False),
        ("http:///no-host", False),
        ("http://name.invalid/t", False),
    ],
)
def test_override_only_on_loopback_or_private(renewer, monkeypatch, override, honoured):
    """IC_RELAY_OAUTH_TOKEN_URL counts only for http(s) on a loopback or private host."""
    monkeypatch.setenv("IC_RELAY_OAUTH_TOKEN_URL", override)
    url = renewer.token_url(CREDENTIALS)
    microsoft = (f"https://login.microsoftonline.com/{CREDENTIALS['tenant_id']}"
                 "/oauth2/v2.0/token")
    assert url == (override if honoured else microsoft)


def test_without_override_the_tenant_is_quoted(renewer, monkeypatch):
    """The tenant goes into Microsoft's URL as one quoted path segment."""
    monkeypatch.delenv("IC_RELAY_OAUTH_TOKEN_URL")
    url = renewer.token_url({"tenant_id": "contoso.onmicrosoft.com/../x?y"})
    assert url == ("https://login.microsoftonline.com/"
                   "contoso.onmicrosoft.com%2F..%2Fx%3Fy/oauth2/v2.0/token")


# --- Nothing secret in the output ---------------------------------------------------------


def test_neither_secret_nor_token_in_output_or_status(renewer, token_server, capsys):
    """The log and status.json never carry the client secret or an access token."""
    token_server.answers = [
        (200, {"access_token": "at-first-token", "expires_in": 3600}),
        (401, _aadsts(7000215, "Invalid client secret provided.")),
        "hang",
        (200, b"{broken"),
        (200, {"access_token": "at-second-token", "expires_in": 3600}),
    ]
    r = renewer.Renewer()
    for _ in range(5):
        r.step()
    out = capsys.readouterr()
    text = out.out + out.err
    with open(renewer.status_path()) as fh:
        text += fh.read()
    for needle in (SECRET, "at-first-token", "at-second-token"):
        assert needle not in text
    assert "token renewed" in out.out
    assert "AADSTS7000215" in out.out


# --- Atomic writes, subcommands, waking ---------------------------------------------------


def test_a_failed_write_leaves_the_old_file(renewer, monkeypatch):
    """If the rename fails, the previous token file stays whole and no temporary is left."""
    r = renewer.Renewer()
    r.step()
    before = _token(renewer)

    real_rename = os.rename

    def broken_rename(src, dst):
        """Fail the token file's rename like a full disk would."""
        if dst == renewer.token_path():
            raise OSError(28, "No space left on device")
        real_rename(src, dst)

    monkeypatch.setattr(renewer.os, "rename", broken_rename)
    assert r.step() == renewer.BACKOFF[0]
    assert _token(renewer) == before
    assert _status(renewer)["error_code"] == "token_write_failed"
    assert not [n for n in os.listdir(renewer.STATE_DIR) if n.endswith(".tmp")]


def test_status_subcommand(renewer, capsys):
    """``status`` prints the status file; before any run, a starting state."""
    assert renewer.main(["ic-relay-oauth", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "starting"}
    renewer.Renewer().step()
    capsys.readouterr()
    renewer.main(["ic-relay-oauth", "status"])
    assert json.loads(capsys.readouterr().out)["state"] == "ok"


def test_unknown_subcommand(renewer, capsys):
    """Anything else prints the usage and exits 2."""
    assert renewer.main(["ic-relay-oauth", "nope"]) == 2
    assert "usage" in capsys.readouterr().err


def test_sigusr1_ends_the_sleep_at_once(renewer):
    """With the signal blocked as run() does, a SIGUSR1 sent to the sleeping thread ends the sleep."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        renewer._prepare_wake_signal()
        assert signal.SIGUSR1 in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        threading.Timer(0.1, signal.pthread_kill,
                        (threading.get_ident(), signal.SIGUSR1)).start()
        start = time.monotonic()
        renewer._sleep(30)
        assert time.monotonic() - start < 5
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def test_sigusr1_sent_before_the_sleep_is_not_lost(renewer):
    """A renewal asked for while a step runs makes the next sleep return at once, once."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        renewer._prepare_wake_signal()
        signal.raise_signal(signal.SIGUSR1)
        start = time.monotonic()
        renewer._sleep(30)
        assert time.monotonic() - start < 5
        start = time.monotonic()
        renewer._sleep(0.3)
        assert time.monotonic() - start >= 0.3
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


_RUN_SCRIPT = """
import grp, importlib.util, os, sys
spec = importlib.util.spec_from_file_location("renewer_run", sys.argv[1])
renewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renewer)
renewer.TOKEN_GROUP = grp.getgrgid(os.getgid()).gr_name
renewer.flush_queue = lambda: None
renewer.run()
"""


def test_run_renews_again_on_a_real_sigusr1_and_ends_on_sigterm(renewer, token_server):
    """``run`` as a process: os.kill(SIGUSR1) renews at once (the next one is 40 min away); SIGTERM exits 0."""
    env = dict(os.environ,
               IC_RELAY_OAUTH_SECRETS=str(renewer.secrets_file),
               IC_RELAY_OAUTH_DIR=renewer.STATE_DIR,
               IC_RELAY_OAUTH_TOKEN_URL=token_server.url)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _RUN_SCRIPT, str(renewer.__file__)], env=env
    )
    try:
        def wait_requests(count):
            """Wait up to 15 s for the endpoint to have been asked ``count`` times."""
            deadline = time.monotonic() + 15
            while len(token_server.requests) < count and time.monotonic() < deadline:
                time.sleep(0.05)
            return len(token_server.requests) >= count

        assert wait_requests(1)
        time.sleep(0.3)  # run() is asleep now: its first step ends with the status write
        os.kill(proc.pid, signal.SIGUSR1)
        assert wait_requests(2)
        proc.terminate()
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
