#!/usr/bin/env bats
# The deletion program of scripts/lib/purge_prefix.sh on an sftp:// DST.
#
# Run against a fake ``paramiko`` backed by a JSON file: the server's
# files (path -> mtime), its folders, its key verdict and its clock.
# Stubbing the library rather than the shell is what makes "the
# successor's files survived" and "a link was not followed" assertions.

setup() {
    LIB="${BATS_TEST_DIRNAME}/../../scripts/lib/purge_prefix.sh"
    CASES="${BATS_TEST_DIRNAME}/../data/sftp_dst_cases.json"
    TMP="$(mktemp -d)"
    export FAKE_SFTP_STATE="$TMP/state.json"
    export HOME="$TMP/home"
    mkdir -p "$HOME/.ssh"
    printf 'backup.example.com ssh-ed25519 AAAA\n' > "$HOME/.ssh/known_hosts"
    : > "$HOME/.ssh/id_ed25519"
    export DST="sftp://u1@backup.example.com/backups/proj/inst"
    unset PURGE_BEFORE PURGE_SINCE BACKEND_PASSWORD

    mkdir -p "$TMP/pylib/paramiko"
    cat > "$TMP/pylib/paramiko/__init__.py" <<'FAKE'
"""Minimal paramiko stand-in: the server is a JSON file."""
import errno
import json
import os
import time

_STATE = os.environ["FAKE_SFTP_STATE"]


def _load():
    with open(_STATE) as fh:
        return json.load(fh)


def _save(data):
    with open(_STATE, "w") as fh:
        json.dump(data, fh)


class SSHException(Exception):
    pass


class BadHostKeyException(SSHException):
    pass


class AuthenticationException(SSHException):
    pass


class RejectPolicy:
    pass


class _Attrs:
    def __init__(self, filename, mode, mtime):
        self.filename = filename
        self.st_mode = mode
        self.st_mtime = mtime


def _missing(path):
    return OSError(errno.ENOENT, "No such file", path)


class _File:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        data = _load()
        data["files"][self.path] = {
            "mtime": time.time() + data.get("skew", 0), "link": False,
        }
        _save(data)

    def write(self, text):
        data = _load()
        data.setdefault("written", []).append(text)
        _save(data)


class _Sftp:
    def lstat(self, path):
        data = _load()
        data.setdefault("seen", []).append(path)
        _save(data)
        if path not in data["dirs"]:
            raise _missing(path)
        return _Attrs(path, 0o040755, 0)

    def stat(self, path):
        data = _load()
        if data.get("stat_fails"):
            raise OSError(errno.EIO, "I/O error", path)
        info = data["files"][path]
        return _Attrs(path, 0o100644, info["mtime"])

    def open(self, path, _mode):
        return _File(path)

    def listdir_attr(self, path):
        data = _load()
        if path not in data["dirs"]:
            raise _missing(path)
        out = []
        for d in data["dirs"]:
            if d != path and d.rsplit("/", 1)[0] == path:
                out.append(_Attrs(d.rsplit("/", 1)[1], 0o040755, 0))
        for f, info in data["files"].items():
            if f.rsplit("/", 1)[0] == path:
                mode = 0o120777 if info["link"] else 0o100644
                out.append(_Attrs(f.rsplit("/", 1)[1], mode, info["mtime"]))
        return out

    def remove(self, path):
        data = _load()
        data["files"].pop(path)
        data.setdefault("removed", []).append(path)
        _save(data)

    def rmdir(self, path):
        data = _load()
        if any(f.startswith(path + "/") for f in data["files"]) or any(
            d.startswith(path + "/") for d in data["dirs"]
        ):
            raise OSError(errno.ENOTEMPTY, "not empty", path)
        data["dirs"].remove(path)
        _save(data)


class SSHClient:
    def set_missing_host_key_policy(self, policy):
        assert isinstance(policy, RejectPolicy)

    def load_host_keys(self, path):
        if not os.path.isfile(path):
            raise OSError(errno.ENOENT, "No such file", path)

    def connect(self, host, port, username, key_filename=None, password=None,
                look_for_keys=True, allow_agent=True, **_kw):
        data = _load()
        data["connected"] = [host, port, username]
        data["auth"] = {
            "key_filename": key_filename, "password": password,
            "look_for_keys": look_for_keys, "allow_agent": allow_agent,
        }
        _save(data)
        verdict = data.get("verdict", "ok")
        if verdict == "badkey":
            raise BadHostKeyException("does not match")
        if verdict == "auth":
            raise AuthenticationException("denied")
        if verdict == "down":
            raise OSError("connection refused")

    def open_sftp(self):
        return _Sftp()

    def close(self):
        pass
FAKE
    export PYTHONPATH="$TMP/pylib"
}

teardown() {
    rm -rf "$TMP"
}

# _seed VERDICT SKEW FILE=MTIME[:link]... — folders are taken from the files;
# an MTIME of ``none`` is a file the server gives no time for.
_seed() {
    python3 - "$@" <<'PY'
import json
import os
import sys

verdict, skew, *files = sys.argv[1:]
data = {"verdict": verdict, "skew": int(skew), "files": {}, "dirs": []}
for arg in files:
    name, _, rest = arg.partition("=")
    mtime, _, link = rest.partition(":")
    data["files"][name] = {
        "mtime": None if mtime == "none" else int(mtime), "link": link == "link",
    }
    parts = name.split("/")
    for i in range(1, len(parts)):
        folder = "/".join(parts[:i])
        if folder and folder not in data["dirs"]:
            data["dirs"].append(folder)
with open(os.environ["FAKE_SFTP_STATE"], "w") as fh:
    json.dump(data, fh)
PY
}

# _set KEY JSON — one more key of the fake server's state.
_set() {
    python3 - "$1" "$2" <<'PY'
import json
import os
import sys

path = os.environ["FAKE_SFTP_STATE"]
data = json.load(open(path))
data[sys.argv[1]] = json.loads(sys.argv[2])
json.dump(data, open(path, "w"))
PY
}

_state() {
    python3 -c "import json,os;d=json.load(open(os.environ['FAKE_SFTP_STATE']));print(json.dumps(d.get('$1')))"
}

_run_program() {
    # shellcheck source=../../scripts/lib/purge_prefix.sh
    source "$LIB"
    ic_purge_prefix_program > "$TMP/prog.py"
    python3 "$TMP/prog.py"
}

@test "sftp: every file under the prefix goes, the sibling stays, folders are removed" {
    _seed ok 0 "backups/proj/inst/a=100" "backups/proj/inst/sub/b=100" "backups/proj/other/keep=100"
    run _run_program
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PURGE_OK removed 2 file(s)"* ]]
    [ "$(_state files)" = '{"backups/proj/other/keep": {"mtime": 100, "link": false}}' ]
    [[ "$(_state dirs)" != *"backups/proj/inst"* ]]
    [ "$(_state connected)" = '["backup.example.com", 22, "u1"]' ]
    [ "$(_state auth)" = "{\"key_filename\": \"$HOME/.ssh/id_ed25519\", \"password\": null, \"look_for_keys\": false, \"allow_agent\": false}" ]
}

@test "sftp: with BACKEND_PASSWORD it logs in with the password and no key" {
    # A destination that logs in with a password ships no key file.
    _seed ok 0 "backups/proj/inst/a=100"
    rm "$HOME/.ssh/id_ed25519"
    export BACKEND_PASSWORD='Correct-horse 9 battery!'
    run _run_program
    [ "$status" -eq 0 ]
    [ "$(_state auth)" = '{"key_filename": null, "password": "Correct-horse 9 battery!", "look_for_keys": false, "allow_agent": false}' ]
    [[ "$output" != *"Correct-horse"* ]]
}

@test "sftp: a refused password is 21 and says it was the password" {
    _seed auth 0 "backups/proj/inst/a=100"
    export BACKEND_PASSWORD='Correct-horse 9 battery!'
    run _run_program
    [ "$status" -eq 21 ]
    [[ "$output" == *"rejected the password"* ]]
    [[ "$output" != *"Correct-horse"* ]]
}

@test "sftp: with a cutoff the successor's files survive" {
    now=$(date +%s)
    _seed ok 0 "backups/proj/inst/old=1000" "backups/proj/inst/new=$((now - 10))"
    export PURGE_BEFORE="$(date -u -d "@$((now - 3600))" +%Y-%m-%dT%H:%M:%S)"
    run _run_program
    [ "$status" -eq 0 ]
    [[ "$output" == *"removed 1 file(s) stored before"* ]]
    [[ "$(_state files)" == *"backups/proj/inst/new"* ]]
    [[ "$(_state files)" != *"backups/proj/inst/old"* ]]
}

@test "sftp: with a since the archived instance's files survive" {
    # The delete of a successor: the archived instance of the same name
    # keeps, in the same folder, what it stored before the successor.
    now=$(date +%s)
    _seed ok 0 "backups/proj/inst/archived=1000" "backups/proj/inst/successor=$((now - 10))"
    export PURGE_SINCE="$(date -u -d "@$((now - 3600))" +%Y-%m-%dT%H:%M:%S)"
    run _run_program
    [ "$status" -eq 0 ]
    [[ "$output" == *"removed 1 file(s) stored since"* ]]
    [[ "$(_state files)" == *"backups/proj/inst/archived"* ]]
    [[ "$(_state files)" != *"backups/proj/inst/successor"* ]]
}

@test "sftp: with a since the server's clock is checked too" {
    _seed ok 600 "backups/proj/inst/successor=1000"
    export PURGE_SINCE="2026-01-01T00:00:00"
    run _run_program
    [ "$status" -eq 22 ]
    [[ "$output" == *"clock"* ]]
    [[ "$(_state files)" == *"backups/proj/inst/successor"* ]]
}

@test "sftp: a server clock off by more than 120 s stops the purge" {
    _seed ok 600 "backups/proj/inst/old=1000"
    export PURGE_BEFORE="2026-01-01T00:00:00"
    run _run_program
    [ "$status" -eq 22 ]
    [[ "$output" == *"clock"* ]]
    [[ "$(_state files)" == *"backups/proj/inst/old"* ]]
}

@test "sftp: the clock probe is an empty file, removed when its time cannot be read" {
    now=$(date +%s)
    _seed ok 0 "backups/proj/inst/old=1000"
    _set stat_fails true
    export PURGE_BEFORE="$(date -u -d "@$((now - 3600))" +%Y-%m-%dT%H:%M:%S)"
    run _run_program
    [ "$status" -eq 22 ]
    [ "$(_state written)" = "null" ]
    [[ "$(_state files)" != *".incubacloud-clock-"* ]]
    [[ "$(_state files)" == *"backups/proj/inst/old"* ]]
}

@test "sftp: with a cutoff a file the server gives no time for is left alone" {
    now=$(date +%s)
    _seed ok 0 "backups/proj/inst/old=1000" "backups/proj/inst/timeless=none"
    export PURGE_BEFORE="$(date -u -d "@$((now - 3600))" +%Y-%m-%dT%H:%M:%S)"
    run _run_program
    [ "$status" -eq 0 ]
    [[ "$output" == *"removed 1 file(s) stored before"* ]]
    [[ "$(_state files)" == *"backups/proj/inst/timeless"* ]]
    [[ "$(_state files)" != *"backups/proj/inst/old"* ]]
}

@test "sftp: an incremental kept that builds on a set it would delete stops the purge whole" {
    # The successor continued the archived chain: its first backup, after
    # the cutoff, is an incremental whose set starts before it.
    now=$(date +%s)
    old=$(date -u -d "@$((now - 7200))" +%Y%m%dT%H%M%SZ)
    new=$(date -u -d "@$((now - 10))" +%Y%m%dT%H%M%SZ)
    _seed ok 0 "backups/proj/inst/duplicity-full.$old.vol1.difftar.gpg=$((now - 7200))" \
        "backups/proj/inst/duplicity-inc.$old.to.$new.vol1.difftar.gpg=$((now - 10))"
    export PURGE_BEFORE="$(date -u -d "@$((now - 3600))" +%Y-%m-%dT%H:%M:%S)"
    run _run_program
    [ "$status" -eq 23 ]
    [[ "$output" == *"IC_PURGE_DEPENDS 1 object(s)"* ]]
    # Only the clock probe went.
    [[ "$(_state removed)" != *duplicity* ]]
    [[ "$(_state files)" == *"duplicity-full.$old"* ]]
}

@test "sftp: an incremental the server gives no time for counts as kept and newer" {
    now=$(date +%s)
    old=$(date -u -d "@$((now - 7200))" +%Y%m%dT%H%M%SZ)
    _seed ok 0 "backups/proj/inst/duplicity-full.$old.vol1.difftar.gpg=$((now - 7200))" \
        "backups/proj/inst/duplicity-inc.$old.to.20991231T000000Z.vol1.difftar.gpg=none"
    export PURGE_BEFORE="$(date -u -d "@$((now - 3600))" +%Y-%m-%dT%H:%M:%S)"
    run _run_program
    [ "$status" -eq 23 ]
    # Only the clock probe went.
    [[ "$(_state removed)" != *duplicity* ]]
    [[ "$(_state files)" == *"duplicity-full.$old"* ]]
}

@test "sftp: a link is removed, never followed" {
    _seed ok 0 "backups/proj/inst/escape=100:link" "backups/proj/elsewhere/data=100"
    run _run_program
    [ "$status" -eq 0 ]
    [[ "$(_state files)" == *"backups/proj/elsewhere/data"* ]]
    [[ "$(_state removed)" == '["backups/proj/inst/escape"]' ]]
}

@test "sftp: a folder that is not there is 10" {
    _seed ok 0 "backups/proj/other/keep=100"
    run _run_program
    [ "$status" -eq 10 ]
    [[ "$output" == *"IC_PURGE_EMPTY"* ]]
}

@test "sftp: another server key is 22, a refused key 21, an unreachable server 22" {
    for verdict in badkey auth down; do
        _seed "$verdict" 0 "backups/proj/inst/a=100"
        run _run_program
        case "$verdict" in
            badkey) [ "$status" -eq 22 ]; [[ "$output" == *"does not match"* ]] ;;
            auth)   [ "$status" -eq 21 ] ;;
            down)   [ "$status" -eq 22 ] ;;
        esac
        [[ "$(_state files)" == *"backups/proj/inst/a"* ]]
    done
}

@test "sftp: no trusted server key file is 22 before connecting" {
    _seed ok 0 "backups/proj/inst/a=100"
    rm "$HOME/.ssh/known_hosts"
    run _run_program
    [ "$status" -eq 22 ]
    [ "$(_state connected)" = "null" ]
}

@test "sftp: the DST reads as the shared table says, a shared folder is refused" {
    # Every case of tests/data/sftp_dst_cases.json: refused ones and
    # folders shorter than {project}/{instance} stop with 22 before
    # connecting; the others connect to the table's server, port and user
    # and look at the table's path.
    source "$LIB"
    ic_purge_prefix_program > "$TMP/prog.py"
    run python3 - "$CASES" "$TMP/prog.py" <<'PY'
import json
import os
import subprocess
import sys

cases = json.load(open(sys.argv[1]))["cases"]
state = os.environ["FAKE_SFTP_STATE"]
bad = []
for dst, expected in cases:
    if dst.startswith("boto3+s3://"):
        # The S3 purge's, not refused here: it stopped with 22 only where
        # boto3 is missing, and runs (1, no bucket) where it is installed.
        continue
    with open(state, "w") as fh:
        json.dump({"verdict": "ok", "skew": 0, "files": {}, "dirs": []}, fh)
    env = dict(os.environ, DST=dst)
    rc = subprocess.run([sys.executable, sys.argv[2]], env=env,
                        capture_output=True).returncode
    seen = json.load(open(state))
    usable = expected and len([s for s in expected[3].split("/") if s]) >= 2
    if not usable:
        if rc != 22 or "connected" in seen:
            bad.append((dst, rc, "should be refused"))
        continue
    user, host, port, path = expected
    if seen.get("connected") != [host, port, user] or seen.get("seen", [None])[0] != path:
        bad.append((dst, seen.get("connected"), seen.get("seen")))
print(bad)
sys.exit(1 if bad else 0)
PY
    echo "$output"
    [ "$status" -eq 0 ]
}
