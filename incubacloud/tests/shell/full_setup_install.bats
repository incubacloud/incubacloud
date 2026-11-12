#!/usr/bin/env bats
# Tests for scripts/full_setup_install.sh.
#
# Every external command is stubbed on PATH so the script's own control
# flow is exercised: the idempotency guards (skip installs when a tool is
# already present) and the unconditional ``apt-get update``. ``sudo`` is
# stubbed to ``env "$@"`` so the ``sudo DEBIAN_FRONTEND=… apt-get …`` form
# resolves to the apt-get stub.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/full_setup_install.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME"
    mkdir -p "$TMP/bin"
    export APT_CALLS="$TMP/apt_calls"
    export IC_DOCKER_DAEMON_JSON="$TMP/daemon.json"
    export TMP
    : > "$APT_CALLS"

    _stub() {  # name, body
        printf '#!/usr/bin/env bash\n%s\n' "$2" > "$TMP/bin/$1"
        chmod +x "$TMP/bin/$1"
    }
    # sudo → run the rest (env handles a VAR=val prefix then the command).
    _stub sudo 'exec env "$@"'
    _stub apt-get 'echo "apt-get $*" >> "$APT_CALLS"; exit 0'
    _stub curl 'exit 0'
    _stub dpkg 'exit 0'
    _stub usermod 'exit 0'
    _stub pipx 'exit 0'
    _stub python3 'exit 0'
    _stub zip 'exit 0'
    _stub logrotate 'exit 0'
    _stub psql 'exit 0'
    _stub crontab 'exit 0'
    _stub systemctl 'echo "systemctl $*" >> "$TMP/systemctl_calls"'
    _stub groups 'echo "cloud docker"'
    _stub docker 'case "$*" in "--version") echo "Docker version 27";; "compose version") exit 0;; *) exit 0;; esac'
    _stub git 'case "$1" in "--version") echo "git version 2.4";; "config") exit 0;; *) exit 0;; esac'
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

# _path_without TOOL: a PATH of the stubs and the system's tools, TOOL left
# out, for a test of a host that lacks it (the runner may well have it).
_path_without() {
    mkdir -p "$TMP/sys"
    local dir tool
    for dir in /usr/local/bin /usr/bin /bin; do
        for tool in "$dir"/*; do
            [ "${tool##*/}" = "$1" ] || [ -e "$TMP/sys/${tool##*/}" ] \
                || ln -s "$tool" "$TMP/sys/${tool##*/}"
        done
    done
    echo "$TMP/bin:$TMP/sys"
}

@test "runs to completion when every tool is already present" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Host toolchain installation complete."* ]]
}

@test "always refreshes the apt index" {
    run bash "$SCRIPT"
    grep -q "apt-get update" "$APT_CALLS"
}

@test "skips apt installs when the tools are already present (idempotent)" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    ! grep -q "apt-get install" "$APT_CALLS"
}

@test "installs the PostgreSQL client the database collector reads with when it is missing" {
    rm "$TMP/bin/psql"
    PATH="$(_path_without psql)" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "apt-get install -y -qq postgresql-client" "$APT_CALLS"
}

@test "installs cron, which runs the agents' collectors, when it is missing" {
    rm "$TMP/bin/crontab"
    PATH="$(_path_without crontab)" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "apt-get install -y -qq cron" "$APT_CALLS"
}

@test "a PostgreSQL client that will not install only warns" {
    rm "$TMP/bin/psql"
    _stub apt-get 'case "$*" in *postgresql-client*) exit 100;; *) exit 0;; esac'
    PATH="$(_path_without psql)" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"postgresql-client install failed"* ]]
}

@test "a host with no containers gets the wide address pools, keeping its options" {
    _stub python3 'exec /usr/bin/python3 "$@"'
    echo '{"live-restore": true}' > "$IC_DOCKER_DAEMON_JSON"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '"10.200.0.0/14"' "$IC_DOCKER_DAEMON_JSON"
    grep -q '"172.17.0.1/16"' "$IC_DOCKER_DAEMON_JSON"
    grep -q '"live-restore": true' "$IC_DOCKER_DAEMON_JSON"
    grep -q "systemctl restart docker" "$TMP/systemctl_calls"
}

@test "a host with no daemon config at all gets one" {
    _stub python3 'exec /usr/bin/python3 "$@"'
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '"10.200.0.0/14"' "$IC_DOCKER_DAEMON_JSON"
}

@test "a host with containers is left alone and says so: a restart would stop them" {
    _stub python3 'exec /usr/bin/python3 "$@"'
    _stub docker 'case "$*" in "ps -aq") echo abc123;; "--version") echo "Docker version 27";; "compose version") echo v2;; *) exit 0;; esac'
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ ! -e "$IC_DOCKER_DAEMON_JSON" ]
    [ ! -e "$TMP/systemctl_calls" ]
    [[ "$output" == *"address pools stay as they are"* ]]
}

@test "a host that has the pools already is not restarted" {
    _stub python3 'exec /usr/bin/python3 "$@"'
    echo '{"default-address-pools": []}' > "$IC_DOCKER_DAEMON_JSON"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ ! -e "$TMP/systemctl_calls" ]
}

@test "pulls the git helper image when given one" {
    _stub docker 'echo "docker $*" >> "$TMP/docker_calls"; case "$*" in "--version") echo "Docker version 27";; *) exit 0;; esac'
    run bash "$SCRIPT" "ghcr.io/tecnativa/doodba:19.0@sha256:abc"
    [ "$status" -eq 0 ]
    grep -qx "docker pull -q ghcr.io/tecnativa/doodba:19.0@sha256:abc" "$TMP/docker_calls"
}

@test "keeps the git helper image under its local tag, with the label the prune spares" {
    _stub docker 'echo "docker $*" >> "$TMP/docker_calls"; case "$*" in "--version") echo "Docker version 27";; "image inspect"*) exit 1;; build*) cat > "$TMP/dockerfile";; *) exit 0;; esac'
    run bash "$SCRIPT" "ghcr.io/tecnativa/doodba:19.0@sha256:0123456789abcdef"
    [ "$status" -eq 0 ]
    grep -qx "docker build -q -t incubacloud-git-helper:0123456789ab -" "$TMP/docker_calls"
    grep -qx "FROM ghcr.io/tecnativa/doodba:19.0@sha256:0123456789abcdef" "$TMP/dockerfile"
    grep -qx "LABEL incubacloud.protect=1" "$TMP/dockerfile"
}

@test "a helper image that cannot be pulled only warns" {
    _stub docker 'case "$*" in "--version") echo "Docker version 27";; pull*) exit 1;; *) exit 0;; esac'
    run bash "$SCRIPT" "ghcr.io/tecnativa/doodba:19.0@sha256:abc"
    [ "$status" -eq 0 ]
    [[ "$output" == *"could not pull the git helper image"* ]]
}

@test "pulls nothing without a helper image" {
    _stub docker 'echo "docker $*" >> "$TMP/docker_calls"; case "$*" in "--version") echo "Docker version 27";; *) exit 0;; esac'
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    ! grep -q pull "$TMP/docker_calls" 2>/dev/null
}

# Full Setup runs the install apart on the server (detached_step.sh, phase 3):
# with the umask of the session that starts it, as attached, no terminal and
# no input. A user that is not root writes /etc through sudo, whose files
# take that umask: 0644, readable by whoever reads them, not the 0600 of the
# script's own files.
@test "run apart as a user that is not root, what it writes through sudo has the session's umask" {
    _stub python3 'exec /usr/bin/python3 "$@"'
    _stub busctl 'case "$*" in *KillUserProcesses*) echo "b false" ;; *) echo "as 0" ;; esac'
    local step="$TMP/.incubacloud-detached-0123456789abcdef" i header=""
    run bash -c "umask 022; bash '${BATS_TEST_DIRNAME}/../../scripts/detached_step.sh' start '$step' 60 host 41 'Install host tools' 'bash $SCRIPT'"
    [ "$status" -eq 0 ]
    for i in $(seq 1 100); do
        header="$(bash "$step/ctl.sh" read "$step" 0 | head -n 1)"
        [[ "$header" == *"state=done "* ]] && break
        sleep 0.1
    done
    [[ "$header" == *"state=done exit=0 "* ]]
    [ "$(stat -c %a "$IC_DOCKER_DAEMON_JSON")" = 644 ]
    [ "$(stat -c %a "$step")" = 700 ]
    bash "$step/ctl.sh" clean "$step"
}
